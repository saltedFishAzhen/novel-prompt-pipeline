# 批量生图：统一入口

从项目根目录 PowerShell 运行。第一次不加 `-Execute`，只检查清单/提示词/引用与依赖，不联网、不加载密钥、不创建执行状态：

```powershell
.\workflows\generate_images.ps1 -Manifest '六位玩家\短剧改编\第三集制作\第三集-生图任务.json'
```

开始生成（默认并发上限 50，单图最多 3 次尝试）：

```powershell
.\workflows\generate_images.ps1 -Manifest '六位玩家\短剧改编\第三集制作\第三集-生图任务.json' -Execute
```

也可以直接用模块（无需 PowerShell 脚本执行策略）：

```powershell
.\.venv\Scripts\python.exe -m openai_images.batch --manifest '清单路径.json' --env-file .env --execute
```

按项目技能分开使用资产清单和分镜清单：先生成场景/人物资产并实看确认，再运行分镜清单，不用依赖调度绕过审阅门。第三集总清单可用于规划/干跑，正式生成应使用本集分阶段清单。第四/第五集只换清单路径。`-Concurrency` 可设 1–50，`-MaxAttempts` 可设 1–10。并发是上限，依赖未完成的任务不会提前出图。50 不是服务端速率承诺。

## 依赖与配置

安装项目的 `images` extra，包含 OpenAI SDK 与 Pillow：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[images]"
```

密钥仅从进程 `OPENAI_API_KEY` 或项目 `.env` 读取，不写日志；既有环境变量优先。只读取 `OPENAI_API_KEY` 与 `OPENAI_IMAGE_MODEL`，不执行 `.env` 中任何代码。官方端点、SDK自动重试为零等限制沿用现有客户端。

## 清单 v1

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "asset-hall",
      "promptFile": "prompts/hall.txt",
      "references": [],
      "outputFile": "assets/hall.png",
      "size": "1536x864",
      "quality": "medium"
    },
    {
      "id": "E03-01-f1",
      "promptFile": "prompts/E03-01-f1.txt",
      "references": ["assets/hall.png", "../角色.png"],
      "outputFile": "frames/E03-01-f1.png",
      "dependsOn": ["asset-hall"],
      "size": "1536x864",
      "quality": "medium"
    }
  ]
}
```

所有相对路径以清单所在文件夹为基准。允许本机绝对路径。提示词 UTF-8；ID只使用ASCII字母数字、点、横线或下划线。每任务一张PNG，顺序传入完整参考图；无参考图调用generate，有参考图调用edit。可逐任务配置model，否则沿现有客户端默认模型/环境变量。size/quality完全由清单决定，不改变项目画幅。

引用本批另一输出时会自动补依赖，也支持显式dependsOn。缺少外部参考、循环依赖、同名输出会在请求前被拒绝。

## 重跑和失败处理

重新运行同一命令可以继续，已存在且可解码图片会跳过（首次接管标记adopted）。记录过成功的文件还会核对哈希。损坏/内容变化、提示词或引用变化不会覆盖或自动重生。

只有服务端明确返回HTTP429且错误码为 `rate_limit_exceeded` 才自动退避重试。遵守数字 Retry-After，否则指数退避加随机抖动；最长等待超过300秒则留为retry_wait，稍后重复同一命令继续等待；总尝试次数跨重启累计，不会重置。

认证/权限/配额/参数拒绝不重试。超时、连接中断、5xx、请求发出后进程退出、回包或落盘不确定，一律needs_attention，不自动重复提交。这不是“所有错误都重试”。检查现有响应后再决定如何处理，不能删状态文件来绕过未知结果。若达到次数上限，可确认仍需继续后提高MaxAttempts；不会影响已成功图片。

每清单旁的 `<清单名>.state/state.json` 保存状态，`responses/<任务ID>/` 保留原响应和run.json。发布目标失败时可从该目录恢复，不需要重新请求。主状态采用线程锁、唯一临时文件及原子替换；批次和每个目标文件采用OS进程锁，退出后自动释放，锁文件本身保留且不需要手工删除。

**切换入口前先结束旧的一次性生图脚本。旧脚本不遵守新锁，不能同时跑同一批。** 不删除已有输出，不自动清理旧记录。此入口不自动刷新故事板、不自动生成视频。

退出码：0=干跑通过或全部完成，2=尚有失败/阻塞/需处理，1=输入/状态/本地环境问题。状态只记录摘要与本地路径，不记录提示词正文、服务端原始错误或密钥。

## 离线验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_openai*.py' -v
```

覆盖50并发上限、多进程锁、断点恢复、明确429有限重试、配额拒绝、超时不重复提交、依赖阻塞、已有文件保护等。测试不请求真实API。
