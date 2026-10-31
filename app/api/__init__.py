"""HTTP 接口层。

main.py 只负责装配（lifespan、静态资源、include_router），
具体业务路由按领域拆到这里，避免入口文件无限膨胀。
"""
