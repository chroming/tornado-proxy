# 基于tornado实现的http代理

fork自https://github.com/boytm/tornado-proxy , 做了一些优化。

编写初衷在于为iOS设备提供性能尚可的HTTP代理（通过pyto运行），当然也可以在其他安装了python3+tornado的环境运行。

优势如下：

1. 第三方库只依赖tornado；
2. 单个代理文件方便直接拷贝使用；
3. 代码量少，便于修改。

## 运行

运行环境Python3+tornado>=6.0

`pip install tornado>=6.0`

python3 proxy.py

## 配置hosts映射

对于某些需要手动配置hosts映射的机器，可以在运行目录下添加`hosts.json`，文件结构如下：

```json 
{
"baidu.com": "192.168.1.1",
"nevelssl1.org": "192.168.1.1"
}
```
