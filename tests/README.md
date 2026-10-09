# PhotoCurator 测试与质量门禁

全部回归测试、基础冒烟测试、Windows UI 冒烟测试、发布门禁及性能结构检查集中存放于此目录，避免污染项目根目录。运行命令请在仓库根目录执行：

```powershell
python tools/repository_audit.py
python -m unittest discover -s tests -p "test_*.py"
python -m tests.release_gate_test
python -m tests.performance_gate_test
python -m tests.windows_smoke_test
python -m tests.ui_smoke_test
```

其中 `ui_smoke_test` 依赖 Selenium 和浏览器驱动；Windows 打包与真实硬件验收应在 GitHub Actions 或 Windows 环境执行，不得将静态检查等同实测。
