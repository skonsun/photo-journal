# 光影 · Photo Journal

把一个本地照片文件夹，变成持续更新的在线摄影作品集。

A lightweight, self-hosted photo gallery that stays in sync with a local folder.

## 功能

- 选择照片库中的文件夹，创建多个独立影集和固定分享链接。
- 自定义标题、封面文案和封面照片。
- 正方形网格，手机两列、电脑多列，按行浏览。
- 按 EXIF 拍摄时间从早到晚排序；缺失时使用文件修改时间。也支持文件名顺序、最新修改优先。
- 每批 24 张，滚动加载或点击“加载更多”。
- 点击查看完整大图，支持上一张、下一张和“查看原图”。
- 文件夹照片增删改自动同步，默认约 15–30 秒反映在浏览页面。
- 后台索引、缩略图预生成、按版本缓存；大影集分批返回。
- 照片目录只读挂载；取消分享不会修改或删除源照片。
- 单管理员密码登录，无需数据库或外部 CDN。

## Docker Compose 安装

需要支持 Compose v2 的 Docker。镜像发布流程构建 linux/amd64、linux/arm64 两种架构。

下载本仓库中的 compose.yaml 与 .env.example，放入同一个项目文件夹：

    cp .env.example .env

编辑 .env，至少修改这两项：

    PHOTO_PATH=/path/to/your/photos
    ADMIN_PASSWORD=你自己设置的至少12位独立密码

密码含有 #、空格等字符时，请按 Compose 的 .env 语法加引号。
程序会拒绝短密码和 CHANGE_ME 开头的占位密码。

启动：

    docker compose pull
    docker compose up -d

打开：

    http://你的NAS地址:8789/admin

在管理页新建分享、选择文件夹、填写标题，保存后复制分享链接。访客通过 /g/随机ID 查看，管理页需要密码。

### 飞牛 fnOS

1. 新建 Docker Compose 项目，导入 compose.yaml。
2. 将 .env.example 复制为 .env，设置本机照片文件夹的绝对路径及管理密码。
3. 启动项目，打开上面的管理地址。
4. 若报目录无权限，为容器 UID/GID 1000 提供读取权限；也可以在 Compose 中用 user 设置你的 NAS 用户 UID/GID。数据卷同样需要该用户可写。不要使用 chmod 777 或挂载整个 NAS 根目录。

只列出 PHOTO_PATH 内部的目录。首次导入大影集需要读取拍摄时间，页面显示索引状态；索引结果保存在数据卷。

### 公网分享

使用反向代理或 Cloudflare Tunnel，把一个域名指向 NAS 的 HTTP 8789 端口，并在公网入口提供 HTTPS。
通过域名打开 /admin，再复制影集链接，链接才会使用公网域名。
程序本身没有 HTTPS 证书管理功能，8789 默认是 HTTP。无需把管理密码发给访客。

## 配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| PHOTO_PATH | 必填 | Compose 宿主机照片路径，只读挂载到 /photos |
| ADMIN_PASSWORD | 必填 | 独立管理密码，至少 12 位 |
| HOST_PORT | 8789 | Compose 对外端口 |
| IMAGE_TAG | latest | 可固定到发布版本 |
| SCAN_INTERVAL | 15 | 扫描间隔秒数，最小 5；前台每 15 秒检查更新 |
| PHOTO_UTC_OFFSET_HOURS | 8 | EXIF 缺少时区时的偏移，以及修改时间显示时区 |
| ALLOW_ORIGINAL | true | 是否允许分享访客打开原始文件 |
| PIP_INDEX_URL | 官方 PyPI | 仅用于源码构建 |

直接运行 Python 时还支持 PHOTO_ROOT、DATA_ROOT 和 PORT；Docker 默认分别为 /photos、/data、8789。

## 照片与隐私

- 支持 JPEG、PNG、WebP、HEIC/HEIF、AVIF、TIFF、BMP；解码能力取决于 Pillow 及 pillow-heif。暂不支持视频和相机 RAW。
- 网格和大图预览使用缩放 JPEG，不包含源文件 EXIF。
- “查看原图”提供未经修改的源文件，可能包含 GPS 等元数据。可设置 ALLOW_ORIGINAL=false 关闭。
- 分享链接无需登录，持有链接的人能查看影集。请只选择准备公开的文件夹；不提供访客密码、多用户权限或逐张审核。
- 取消分享会阻止后续请求，但无法收回访客已保存或缓存的照片。
- 图片缓存在数据卷 cache/ 下，源照片始终只读。大型照片库应关注数据卷磁盘空间；缓存可在停止服务后删除重建。
- 不要提交 .env、照片、数据卷、NAS 凭据或隧道令牌到仓库。

## 更新、备份与恢复

    docker compose pull
    docker compose up -d

备份 .env 和 journal-data 数据卷。galleries.json 保存影集及固定分享 ID；
dates.json、index/、cache/ 可重新生成。恢复相同配置和 galleries.json，分享 ID 不变。

管理密码修改后重新创建容器，旧管理会话失效。照片目录内容变动不需要重新生成分享链接。

## 从源码构建

    git clone https://github.com/skonsun/photo-journal.git
    cd photo-journal
    cp .env.example .env
    # 编辑 .env 后：
    docker compose -f compose.yaml -f compose.build.yaml up -d --build

若官方 PyPI 不可达，可自行修改 .env 的 PIP_INDEX_URL 使用可信镜像。
开发与回归测试：

    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v

## 自动发布

GitHub Actions 对 PR 运行测试；main 和版本标签通过测试后构建并推送 GHCR。
v0.1.0 这样的标签生成对应版本镜像；main 更新 latest。
首次发布包需在 GitHub Packages 中设置 Public，才能匿名拉取。

代码采用 MIT 许可。Python、Pillow、pillow-heif 及其依赖遵循各自许可证。
