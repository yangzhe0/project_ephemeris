# 天文卫星星历与寻星图工具 (Ephemeris & Finder Chart Tool)

这是一个基于 Python Flask 的 Web 应用程序，专为天文观测者设计。它可以从莫斯科国立大学 (SAI MSU) 服务器获取高精度的卫星星历数据，并结合 STScI 的 DSS (数字化巡天) 图像生成实时的寻星图，帮助用户在夜空中精确定位木星、土星、天王星和海王星的卫星。

## 🚀 主要功能

*   **高精度星历计算**: 
    *   对接 SAI MSU `nss-eph3` 接口。
    *   支持自定义观测站 (Observatory Code)、具体时间 (UTC) 和目标天体。
    *   提供多种历表模型选择 (DE441, DE431, INPOP19a 等)。
*   **自动化寻星图生成**:
    *   根据计算出的赤经 (R.A.) 和赤纬 (Dec.) 自动从 STScI 下载 DSS 图像。
    *   支持自定义视场 (FOV) 大小 (角分)。
*   **现代化交互界面**:
    *   基于 Tailwind CSS 的响应式设计。
    *   **一键复制**: 快速复制坐标数据用于望远镜控制软件。
    *   **交互式预览**: 支持图片全屏查看、缩放、拖拽和平移。
    *   **结果导出**: 支持下载生成的寻星图。

## 🛠️ 技术栈

*   **后端**: Python 3, Flask
*   **数据处理**: Requests (HTTP请求), BeautifulSoup4 (HTML解析)
*   **前端**: HTML5, JavaScript (原生), Tailwind CSS (CDN)
*   **数据源**:
    *   星历数据: [SAI MSU Natural Satellites Ephemeride Server](https://www.sai.msu.ru/neb/nss/nss_ephce.htm)
    *   星图数据: [STScI Digitized Sky Survey](https://archive.stsci.edu/cgi-bin/dss_form)

## 📦 安装与运行

### 1. 环境要在

确保已安装 Python 3.8 或更高版本。

### 2. 安装依赖

在项目根目录下运行以下命令安装所需的 Python 库：

```bash
pip install -r requirements.txt
```

*注意: `requirements.txt` 应包含 `flask`, `requests`, `beautifulsoup4` 等。*

### 3. 启动应用

运行 `app.py`:

```bash
python app.py
```

终端将显示如下信息：
```
 * Running on http://0.0.0.0:8000/ (Press CTRL+C to quit)
```

### 4. 访问

打开浏览器访问 [http://localhost:8000](http://localhost:8000) 即可使用。

## 📖 使用指南

1.  **设置参数**:
    *   **观测站**: 输入你的观测站代码 (如云南天文台 1米镜为 286)。
    *   **目标天体**: 选择你想观测的卫星 (如土卫六 S6, 木卫六 J6 等)。
    *   **时间**: 默认使用当前 UTC 时间，也可手动修改。
2.  **获取数据**:
    *   点击“开始计算”，系统将从 MSU 获取数据。
3.  **查看与寻星**:
    *   结果列表中会显示具体时刻的赤经赤纬。
    *   点击“预览DSS”查看该坐标点的星图，确认目标位置。
    *   调整视场 (FOV) 数值以匹配你的望远镜或相机视野。

## 📝 注意事项

*   本工具依赖外部 API (MSU 和 STScI)，请确保网络连接正常。
*   第一次加载某一天区的 DSS 图像时可能需要几秒钟下载，后续如有缓存会更快。
