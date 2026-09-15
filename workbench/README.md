# Yue 2.0 · Token Monster 音乐工作台

填写音乐风格和歌词，使用云实例上的 YuE2 生成音乐。当前是直接输入版，已移除 AI 构思、对话、修改建议和大模型 API 配置入口。

## 在镜像中启动

通过 AutoDL 的 WebUI-6006 入口访问。当前是免登录的私人工作空间，能够访问入口的人使用同一作品库。

```bash
bash /root/YuE/workbench/control.sh start
bash /root/YuE/workbench/control.sh status
bash /root/YuE/workbench/control.sh stop
bash /root/YuE/workbench/control.sh restart
curl http://127.0.0.1:6006/api/health
```

镜像已有的开机钩子会启动服务。保存过的旧镜像不会随 GitHub 更新，需要更新实例后重新保存镜像。

## 从源码安装

按照仓库根目录的官方说明安装 YuE2 到 `/root/YuE/.venv`，然后安装工作台依赖：

```bash
cd /root/YuE
.venv/bin/python -m pip install -r workbench/requirements.txt
bash workbench/control.sh start
```

工作台的运行脚本面向 `/root/YuE` 的镜像目录布局。其他目录部署需调整 `run.sh`、`control.sh` 中的路径。自行安装源码不会自动创建平台开机钩子。

## 自动准备模型

启动后检查本地缓存，通过校验的文件直接复用。缺失时优先接入平台已挂载的 BF16 公共模型；共享文件不可用或校验不匹配时，自动从 HF-Mirror 下载固定官方版本。

- YuE2-3B：`1a96eca688d6ae5d7f0feb88573fec89920fcd19`。
- YuE2-Vae：`95535e72a97bc0f09b8ada125d26b4009428c0e8`。
- 共约 7.79 GB，另需至少 1 GiB 空余；默认缓存 `/root/autodl-tmp/huggingface/hub`。
- 镜像不能携带旧实例的外部模型挂载；没有共享文件的新实例会走网络下载，不需要手动执行软链接命令。
- 页面显示实际下载字节，支持暂停、续传、手动切换下载来源和官方摘要校验。没有虚构百分比或剩余时间。
- 下载不占用 GPU；点击生成后才加载推理模型。
- 详细说明见 [MODEL_SETUP.md](MODEL_SETUP.md)。

## 创作流程

1. 新建歌曲，在手稿内填写音乐风格。
2. 粘贴歌词，使用 `[Verse]`、`[Chorus]` 等段落标记；可点击“整理歌词段落”。
3. 默认使用完整规划，点击“生成歌曲”。也可以先单独生成音乐方案。
4. 下方显示排队、模型加载、音乐规划、音频生成、合成、解码和完成状态。
5. 完成后试听、跳转、下载音频，或选择已有版本继续修改。

高级设置仅保留生成方式、种子与 ABC 乐谱。已有乐谱想进一步修改，或想翻唱、尝试新的音乐风格，可以输入或粘贴到 ABC 乐谱框，再检查输入。

- `full`：默认，使用旋律与和弦规划。
- `melody`：使用旋律规划，伴奏自由发挥；输入此模式的乐谱应不带和弦符号。
- `off`：直接根据歌词和风格生成，不能同时提供外部 ABC。

作品概要、语言、情绪、人声等备注继续保存；需要影响实际生成的内容请写入音乐风格。旧版本中的笔记和参数不会因界面精简而被覆盖。

修改乐谱后生成的是新的完整录音，不保证其他位置的原始波形保持不变。当前没有参考音频上传、SheetSage2 转录或听觉分析功能。这里的翻唱入口使用已有 ABC 乐谱。

## 存储与验证

- 项目、版本、任务、作品：`/root/autodl-tmp/yue2-studio`。
- 日志：`/root/autodl-tmp/yue2-studio/service.log`。
- 单卡推理队列并发为 1，普通页面操作不等待 GPU 任务。
- 健康检查：`/api/health`。

```bash
cd /root/YuE
.venv/bin/python -m unittest discover -s workbench -p 'test_*.py' -q
.venv/bin/python -m yue2.cli --help
```

本次待发布代码通过 43 项测试；浏览器验证了歌词编辑、整理、保存、刷新恢复、输入校验及无歌词提交拦截。自动准备功能在云实例验证了完整缓存复用、真实配置文件下载校验，以及权重从 2 MiB 续传到 4 MiB。本轮界面精简未重新生成完整音频。

仓库保留未被应用加载的旧创作服务适配模块和独立测试，供已有项目兼容与维护；生产服务不再注册相关 API。

## 分享镜像

先停止服务，再清理本地模型缓存、私人作品和密钥。停止后不要再次启动工作台，否则会自动重新准备模型。保留代码中的 `model-assets`；其中不包含模型权重。

工作台代码与算法、模型许可分别适用。官方代码许可见根目录 LICENSE，模型使用条件见各模型仓库及 `model-assets` 内保留的许可文件。
