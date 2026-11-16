# 第三集整集制作

唯一整集报告：第三集-storyboard-report.html。
5场62拍，一份故事板，连续E03-01至E03-18，181.8秒。禁止拆场目录。
离线结构机器门已过（4新景已实际认图，跨图全局罗盘连接不冒认）；4新景已生成并实看认图，62分镜图未完成，视频未生成。
逐镜挂景读取第三集-逐切环境引用.json；包内manifest的prompt/pictures/missing使用E03-xx/文件名相对路径。
资产引用两份归资产作者，生成和刷新脚本归父任务。详见三份handoff。

## 统一生图入口
先第三集-场景生图任务.json（4 项），实看新图并更新站位后，再第三集-分镜生图任务.json（62 项）。66 项总清单仅供规划。
统一工具 workflows/generate_images.ps1 -Manifest <清单> 默认本地 dry-run；加 -Execute 才会上传参考图并调用付费服务。并发上限 50，场景实际最多 3 个并行。勿与旧 generate_episode3_assets.py 同时运行，未知请求不能擅自重复。
refresh_episode3.py 只本地刷新整集 HTML 和缺图标记，不调用 API 或打开网页。
prepare_episode3_batch.py 只重建离线清单与提示词。
