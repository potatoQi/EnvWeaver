# Runtime

推荐在 Pipeline 中显式创建 `Runtime(root)`，明确资产位置。
`from envweaver import runtime` 提供同名便捷函数；start 使用默认存储目录，
其余实例操作从 `instance.root` 取得存储位置。自定义后端应通过注册它的 Runtime 对象调用。

Docker / QEMU 的配置及前置条件见[使用指南](../usage.md)。

::: envweaver.Runtime
    options:
      members: [register, start, exec, upload, download, save, close, connect, describe]
