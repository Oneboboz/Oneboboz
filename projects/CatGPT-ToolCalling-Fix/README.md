# CatGPT Tool Calling Loop Fix V2

针对 [TheBadFella/CatGPT](https://github.com/TheBadFella/CatGPT) 浏览器模式的 OpenAI-compatible Tool Calling 循环修复。

## 修复内容

1. 保留最新用户回合中的 `assistant.tool_calls`，避免 latest-turn 裁剪后丢失上一轮工具调用。
2. 已存在 `role=tool` 结果时，明确告诉网页模型不要重复已经执行的工具。
3. 当客户端继续携带 `tool_choice=required` 时，只要已有工具结果，不再机械要求再次调用工具。
4. 检测完全相同的 `tool + arguments` 重复调用；检测到循环后转入最终自然语言回答。
5. 修改前自动备份 `src/api/openai_routes.py`。
6. 自动执行 Python 语法检查。

## 使用

把 `patch_toolcalling.py` 复制到 CatGPT 源码根目录，然后执行：

```powershell
python .\patch_toolcalling.py
```

确认出现：

```
[OK] Python 语法检查通过。
[OK] Tool Calling V2 修复完成。
```

重新构建并启动：

```powershell
docker compose build --no-cache
docker compose up -d
docker compose ps
```

## Zoo Code 测试

发送：

> 请使用一次 list_files 工具查看当前工作目录，然后告诉我结果。

预期流程：

```
Zoo Code
  -> CatGPT
  -> tool_calls: list_files
  -> Zoo Code 执行工具
  -> role=tool 返回结果
  -> CatGPT 直接总结结果
```

不应再次出现：

```
list_files
  -> list_files
  -> list_files
  -> ...
```

## 恢复

补丁会生成：

```
src/api/openai_routes.py.backup-YYYYMMDD-HHMMSS
```

恢复时用备份覆盖修改后的 `src/api/openai_routes.py`。

## 说明

这个补丁不会关闭 Tool Calling，只针对已经执行完成的相同工具调用循环做保护。
