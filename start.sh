#!/usr/bin/env bash
set -e

# 安装 Playwright 浏览器二进制（仅需执行一次，每次容器启动也可以执行以确保存在）
python -m playwright install chromium

# 启动主程序
python main.py
