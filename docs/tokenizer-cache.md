# 本地用量统计

镜像构建时通过 `scripts/prepare_tokenizers.py` 下载并打包 o200k_base 和 cl100k_base。
下载失败会中止构建，避免部署不完整的镜像。

启动时校验文件 SHA-256，并把计数文件复制到 `/app/data/tiktoken-cache`。
继续挂载原来的 `/app/data` 即可在更新容器后保留缓存。
缓存缺失或损坏时从镜像中的文件恢复；目录不可写时直接读取镜像中的文件。
请求期间不会下载计数文件。可通过 `TIKTOKEN_CACHE_DIR` 和 `TIKTOKEN_BUNDLE_DIR` 设置目录。

本地开发可执行 `.venv/Scripts/python.exe scripts/prepare_tokenizers.py data/tokenizer-bundle`，
并将 `TIKTOKEN_BUNDLE_DIR` 指向该目录。

图片接口的 usage 是本地统计，不是 ChatGPT 账号剩余额度。
文本计数不可用时按 UTF-8 字节数 / 4 向上取整估算，返回
`usage.estimated: true` 与 `usage.estimated_fields`，日志记录 `usage_estimated`。
图片计数发生异常时对应字段暂填 0，并通过 `usage.unavailable_fields` 指出不可用字段。
这些异常不会使已经成功生成的图片变成失败响应，所有生成结果仍然返回。
正常情况下保留原来的 usage 格式。
