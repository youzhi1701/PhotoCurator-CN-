# 测试目录

本目录统一存放原先堆在项目根目录的 Python 回归测试。运行时请从项目根目录执行，例如：

```powershell
python -m unittest -v tests.test_exact_duplicates
python -m unittest discover -s tests -p "test_*.py"
```

`quality_annotations_input_test.py` 同样属于回归测试。独立的发布/性能/UI 冒烟脚本暂保留根目录，以避免改变已有入口；后续搬迁需要同步 Windows 打包及所有执行路径。
