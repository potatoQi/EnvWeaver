# 文档维护

## 原理

```text
Python 签名、类型、docstring → mkdocstrings → API 页面
Markdown 使用指南          → MkDocs       → 同一个文档网站
```

用户手册集中在 index.md。每个接口先给调用示例，再在 details 折叠块里引用代码对象，例如：

```markdown
::: envweaver.TaskPool
```

构建时会从 `src/` 静态读取该对象的代码与说明，不需要启动 Docker、QEMU 或模型服务。
工具不会从代码推断所有设计约定：用途、运行位置、保存范围和错误语义必须写进 docstring。

## 预览与构建

在 EnvWeaver 仓库根目录运行：

```bash
uv sync --group docs
uv run --group docs mkdocs serve
```

打开 http://127.0.0.1:8000 。保存 Markdown 或 Python 源码后会重新构建。

```bash
uv run --group docs mkdocs build --strict
```

静态网站写入 `site/`，不提交生成文件。文档依赖独立放在 docs dependency group，
普通 pip 用户不会因此安装 MkDocs。CI 自动构建检查，不自动发布网站。

## 修改代码时

1. 修改公开接口时，同时更新类型标注及 Google 风格 docstring 的 Args、Returns、Raises。
2. 新增公开对象时，在 index.md 对应的折叠块中加入引用，不为每个对象新建页面。
3. 使用方式或约定发生变化时，更新受影响的指南。
4. 执行严格文档构建和与改动相关的测试。

文档检查能发现接口引用和链接问题，不能证明描述与运行行为完全一致。
代码与文档应在同一次修改中接受 review，必要时由功能测试验证约定。

工具说明：[MkDocs](https://www.mkdocs.org/)、[mkdocstrings Python](https://mkdocstrings.github.io/python/usage/)。
