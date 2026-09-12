# 余音 · YuE2 AI 音乐创作工作台

面向不懂乐理的创作者：一句想法 → 比较创作方向 → 编辑歌词 → 生成歌曲 → 试听 → 修改后生成新版本。

本目录是基于 YuE2 的非官方中文工作台。底层算法来自 [multimodal-art-projection/YuE](https://github.com/multimodal-art-projection/YuE)，本仓库保留其源代码、历史与许可证。本项目使用 YuE2，非 YuE-v1。

## 在余音镜像中使用

实例开机后，通过 AutoDL 的 **WebUI-6006** 入口访问。当前是免登录的私人工作空间；能够访问入口的人使用同一作品库。

```bash
bash /root/YuE/workbench/control.sh start
bash /root/YuE/workbench/control.sh status
curl http://127.0.0.1:6006/api/health
```

首次打开“创作助手设置”，填写自己的 OpenAI-compatible Base URL、模型名称和 API Key，再测试连接。没有 API 时仍可手动填写歌词和风格生成音乐。API 服务的费用由所选服务商决定。

密钥在服务器加密保存，不在网页明文回显，不默认放入 localStorage；支持删除配置。不要将作品数据库、加密密钥或访问凭据提交到 GitHub。

## 创作流程

1. 新建歌曲，输入“送给毕业好友，温暖、不太伤感，先做一段短歌”等想法。
2. 点击“一起构思”，比较助手提供的方向；检查建议与差异后应用。
3. 编辑歌名、概要、分段歌词和音乐方向。手稿自动保存，支持撤销。
4. 点击“生成歌曲”，查看真实的排队、加载、规划、生成、合成、解码状态。
5. 完成后播放、跳转、下载音频；波形来自真实音频。
6. 选择“从此版继续修改”，用自然语言提出反馈，应用建议后生成新版本。

普通对话不调用 GPU，只有明确的生成操作才创建任务。单卡串行执行，项目、任务和版本使用 SQLite 持久化。

## 模式与边界

- `full`：默认，规划旋律与和弦。
- `melody`：旋律规划，伴奏自由生成。
- `off`：直接依据歌词和风格生成。

这些模式不代表速度或音质等级。ABC 乐谱位于高级视图，编辑后先做语法和模式校验。乐谱修改生成的是新的完整录音，不保证未编辑段的原始波形保持不变。风格提示不保证精确控制编曲。工作台没有实际试听分析能力，也未接入参考音频上传或 SheetSage2 转录。

## 镜像审核和环境验证

镜像中算法仓库位于 `/root/YuE`，Python 环境位于 `/root/YuE/.venv`。

```bash
cd /root/YuE
.venv/bin/python -m yue2.cli --help
.venv/bin/python -m yue2.cli doctor --offline
cd workbench
../.venv/bin/python -m unittest test_harness -v
```

`doctor` 的 `dependencies_ready: true` 表示依赖就绪；其 `validated: false` 不表示完成了音质或最低显存验收。基础检查不会下载模型或触发音乐生成。

已部署环境：Python 3.10、yue2-infer 0.1.6、PyTorch 2.10.0+cu130、Transformers 4.57.6；实测 GPU 为 RTX 4080 SUPER，驱动报告显存 32760 MiB。基础镜像标注的 CUDA 11.8 与实际 PyTorch 自带的 CUDA 13.0 运行时是不同信息。

工作台验证基线为官方提交 `92a73cc7652fcc1f937855e4b765e0a0edd7ff2e`。此 GitHub 派生仓库创建时继承更新的官方 main；不要把仓库最新 HEAD 当作已在该镜像完整回归过的版本。已验证真实创作 API、full 模式歌曲生成、两版试听下载、刷新恢复、方案复用和取消；部署版本的 16 项回归检查通过。新克隆环境需重新验证。

## 从源码安装

在全新的 Linux 环境中，将本仓库克隆到 `/root/YuE`，按照根目录官方 README 创建 `.venv` 并安装 YuE2。不要在已有部署上覆盖目录或重建环境。

```bash
git clone https://github.com/enter-presser/yue2-yuyin-studio.git /root/YuE
# 按官方说明完成 .venv 和 YuE2 安装后：
cd /root/YuE
.venv/bin/python -m pip install -r workbench/requirements.txt
chmod +x workbench/*.sh
bash workbench/control.sh start
```

`run.sh` 默认使用 `/root/autodl-tmp/huggingface` 缓存并启用离线模式。余音镜像已准备权重；新环境需依据官方指南自行准备匹配的 YuE2-3B 与 YuE2-Vae 权重，再开启服务。本仓库不包含权重或 API 凭据，也不会由开机脚本自动申请付费资源。

## 代码和数据

| 文件 | 职责 |
| --- | --- |
| `static/` | 中文创作界面、播放器、版本切换 |
| `server.py` | HTTP API、项目与版本、访问校验 |
| `assistant.py` | OpenAI-compatible 适配与受约束创作工具 |
| `domain.py` | 输入 schema、歌词、参数、ABC 校验 |
| `worker.py` | 单卡队列、四阶段推理、取消与阶段产物 |
| `store.py` | SQLite、加密凭据、输入与任务追踪 |
| `test_harness.py` | 隔离的回归和故障测试，不作为音乐生成演示 |

运行数据默认位于 `/root/autodl-tmp/yue2-studio/`：`studio.sqlite3` 存储项目和任务，`artifacts/` 存储产物，`service.log` 存储日志。`master.key` 在首次保存 API 配置时才创建。保管备份时数据库和加密密钥应配套保存。

```bash
bash /root/YuE/workbench/control.sh stop
bash /root/YuE/workbench/control.sh restart
```

镜像里的 `rollback.sh` 依赖部署时的原界面备份，仅适用于保留该备份的实例；新克隆仓库不能凭空恢复旧部署。保存镜像前应清除个人配置，确认是否保留作品，并核实平台对系统盘与数据盘的保存范围。

## 许可证

保留并遵守仓库根目录的 `LICENSE`、`MODEL_LICENSE` 和 `THIRD_PARTY_NOTICES.md`。官方代码与模型权重的许可证不同；请阅读原文。本工作台不代表 YuE 官方产品或授权背书。
