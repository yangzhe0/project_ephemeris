# 这是一个基于Flask的Web后端，用于接收前端请求并查询MSU天文台卫星星历数据
from flask import Flask, render_template, request, jsonify, send_file
import tempfile
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime, timezone
import traceback
import os
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 全局配置与线程池 ---
executor = ThreadPoolExecutor(max_workers=5)
dss_cache = {}

# --- 辅助函数 ---

def download_dss_image(ra, dec, output_file, height=7, width=7, survey='poss2ukstu_red', file_format='gif'):
    """
    下载DSS图片的辅助函数
    """
    # 确保 requests 可用 (防止作用域问题)
    import requests

    # 检查内存缓存 (简单的路径缓存)
    cache_key = f"{ra}_{dec}_{height}_{width}"
    if cache_key in dss_cache and os.path.exists(dss_cache[cache_key]):
        # 如果缓存文件路径与目标不同，则复制
        if dss_cache[cache_key] != output_file:
            try:
                with open(dss_cache[cache_key], 'rb') as src, open(output_file, 'wb') as dst:
                    dst.write(src.read())
                return True
            except Exception as e:
                print(f"Cache copy failed: {e}")
                # 复制失败则继续下载
        else:
            return True

    base_url = "https://archive.stsci.edu/cgi-bin/dss_search"
    
    params = {
        'v': survey,
        'r': ra,
        'd': dec,
        'e': 'J2000',
        'h': height,
        'w': width,
        'f': file_format,
        'c': 'none',
        'fov': 'NONE',
        'v3': ''
    }
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        print(f"Downloading DSS: {ra}, {dec} -> {output_file}")
        response = requests.get(base_url, params=params, headers=headers, stream=True, timeout=60)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '')
        if 'image' not in content_type and 'fits' not in content_type:
            print(f"Invalid content type: {content_type}")
            return False

        with open(output_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        # 存入缓存
        dss_cache[cache_key] = output_file
        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False

def preload_dss_images(results, fov=7):
    """异步预加载 DSS 图片"""
    temp_dir = tempfile.gettempdir()
    
    for item in results:
        ra = item['ra_pure']
        dec = item['de_pure']
        
        # 构造一个通用的缓存文件名
        cache_filename = f"dss_cache_{ra.replace(' ', '')}_{dec.replace(' ', '')}_{fov}.gif"
        filepath = os.path.join(temp_dir, cache_filename)
        
        # 提交到线程池
        executor.submit(download_dss_image, ra, dec, filepath, height=fov, width=fov)

def fetch_satellite_data(plnvar, satellite, nde, observatory, initmom, ntimes, timestep):
    """
    核心爬虫函数：发送POST请求到MSU服务器并解析返回的HTML
    """
    try:
        # 构建请求参数
        payload = {
            'langue': '30',
            'plnvar': plnvar,
            'satellite': satellite,
            'relative': '-1',
            'nde': nde,
            'observatory': observatory,
            'epoch': 'ICRF',
            'tscale': 'UTC',
            'initform': '1',
            'initmom': initmom,
            'steptype': '1',
            'timestep': timestep,
            'ntimes': ntimes,
            'outputtype': '0',
            'vangle': '0'
        }

        print(f"Requesting MSU API: {payload}")

        response = requests.post(
            'https://www.sai.msu.ru/neb/nss/cgi-bin/nss-eph3.cgi',
            data=payload,
            timeout=15
        )
        response.raise_for_status()

        # 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        pre_tag = soup.find('pre')

        if not pre_tag:
            return {'success': False, 'message': '未找到数据区域(Pre tag missing)'}

        pre_content = pre_tag.text
        results = []

        # 逐行解析数据
        for line in pre_content.splitlines():
            # 匹配类似 2024 11 25 ... 的行
            if re.match(r'\d{4}\s+\d+\s+\d+', line.strip()):
                parts = re.split(r'\s+', line.strip())
                # 确保有足够的列
                if len(parts) >= 12:
                    # 提取时间和坐标
                    time_str = f"{parts[0]}-{parts[1]}-{parts[2]} {parts[3]}:{parts[4]}:{int(float(parts[5]))}"
                    alpha = f"{parts[6]}h {parts[7]}m {parts[8]}s"
                    delta = f"{parts[9]}° {parts[10]}' {parts[11]}\""
                    
                    # 纯数值格式
                    alpha_pure = f"{parts[6]} {parts[7]} {parts[8]}"
                    delta_pure = f"{parts[9]} {parts[10]} {parts[11]}"

                    results.append({
                        'time': time_str,
                        'ra': alpha,
                        'de': delta,
                        'ra_pure': alpha_pure,
                        'de_pure': delta_pure,
                        'raw_line': line
                    })

        if not results:
            return {'success': False, 'message': '未从返回内容中解析到星历数据', 'raw_response': pre_content[:200]}

        # 触发预加载 (默认 FOV 7)
        preload_dss_images(results)

        return {'success': True, 'data': results}

    except requests.Timeout:
        return {'success': False, 'message': '请求超时，请检查网络或重试'}
    except Exception as e:
        traceback.print_exc()
        return {'success': False, 'message': f'发生错误: {str(e)}'}

# --- 常量定义 ---

SATELLITES = [
    {'name': 'S0 (6000)', 'code': '6000'},
    {'name': 'S8 (6008)', 'code': '6008'},
    {'name': 'S9 (6009)', 'code': '6009'},
    {'name': 'J0 (5000)', 'code': '5000'},
    {'name': 'J6 (10001)', 'code': '10001'},
    {'name': 'J7 (10002)', 'code': '10002'},
    {'name': 'J8 (10003)', 'code': '10003'},
    {'name': 'J9 (10004)', 'code': '10004'},
    {'name': 'N1 (8001)', 'code': '8001'},
    {'name': 'N2 (8002)', 'code': '8002'},
    {'name': 'U0 (7000)', 'code': '7000'},
]

# OBSERVATORIES list is no longer needed for backend validation 
# as the frontend will send raw code/text. 
# But we can keep it if we want to provide some defaults, 
# but user asked to change frontend to input box.
# So I will remove it from here to keep it clean.

# --- 路由定义 ---

@app.route('/')
def index():
    """渲染主页"""
    return render_template('index.html', 
                         satellites=SATELLITES,
                         default_satellite='6000')

@app.route('/api/calculate', methods=['POST'])
def calculate():
    """API接口：接收JSON数据并返回计算结果"""
    data = request.json
    satellite = data.get('satellite')
    observatory = data.get('observatory')
    initmom = data.get('initmom')
    plnvar = data.get('plnvar', '0')
    nde = data.get('nde', '6')
    ntimes = data.get('ntimes', '1')
    timestep = data.get('timestep', '1')

    if not all([satellite, observatory, initmom]):
        return jsonify({'success': False, 'message': '缺少必要参数 (卫星、观测站或时间)'})

    result = fetch_satellite_data(plnvar, satellite, nde, observatory, initmom, ntimes, timestep)
    return jsonify(result)

@app.route('/api/download_chart', methods=['POST'])
def download_chart():
    """API接口：下载DSS证认图并返回给前端"""
    data = request.json
    ra = data.get('ra')
    dec = data.get('dec')
    satellite_name = data.get('satellite_name', 'Unknown')
    time_str = data.get('time_str', '')
    fov = data.get('fov', 7)
    
    if not ra or not dec:
        return jsonify({'success': False, 'message': '缺少必要参数 (赤经或赤纬)'})

    # 转换为 float
    try:
        fov_val = float(fov)
    except:
        fov_val = 7.0

    # 格式化文件名 (加入 FOV 防止浏览器缓存)
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        formatted_time = dt.strftime("%Y%m%d_%Hh%Mm%Ss")
    except:
        formatted_time = datetime.now().strftime("%Y%m%d_%Hh%Mm%Ss")
        
    sat_clean = satellite_name.split('(')[0].strip()
    # 使用 g/G 格式化自动去掉不必要的 .0，或者直接用 fov_val 
    # 为了文件名整洁，如果接近整数显示整数
    if abs(fov_val - round(fov_val)) < 0.001:
        fov_str = f"{int(round(fov_val))}"
    else:
        fov_str = f"{fov_val}"
        
    filename = f"{sat_clean}_{formatted_time}_fov{fov_str}.gif"
    
    # 使用临时文件
    temp_dir = tempfile.gettempdir()
    filepath = os.path.join(temp_dir, filename)

    success = download_dss_image(ra, dec, filepath, height=fov_val, width=fov_val)
    
    if success:
        download_url = f"/api/get_file?path={filepath}&filename={filename}"
        return jsonify({
            'success': True, 
            'download_url': download_url,
            'filename': filename
        })
    else:
        return jsonify({'success': False, 'message': '从DSS服务器获取图片失败'})

@app.route('/api/get_file')
def get_file():
    """提供文件下载"""
    filepath = request.args.get('path')
    filename = request.args.get('filename')
    
    if not filepath or not os.path.exists(filepath):
        return "File not found", 404
        
    return send_file(filepath, as_attachment=True, download_name=filename)

@app.route('/robots.txt')
def robots():
    """响应 robots.txt"""
    return "User-agent: *\nDisallow:", 200, {'Content-Type': 'text/plain'}

@app.route('/favicon.ico')
def favicon():
    """响应 favicon.ico"""
    return send_file(os.path.join(app.root_path, 'static', 'favicon.png'), mimetype='image/png')

@app.route('/favicon.png')
def favicon_png():
    """响应 favicon.png"""
    return send_file(os.path.join(app.root_path, 'static', 'favicon.png'), mimetype='image/png')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
