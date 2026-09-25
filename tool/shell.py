#!/usr/bin/env python3
"""One small command tree; all mutations use task_queue's checked transaction path."""
import sys
from task_queue import main

if __name__ == '__main__':
    if sys.argv[1:] in ([], ['--help'], ['-h']):
        print('''用法：python3 tool/shell.py [--root 模板根] [--wait 秒] <命令组> ...
  init / doctor / check [--staged] / repair
  task register / approve / claim / deliver / pass / status / history
  task revise / replan / rework / handoff / release / revoke / cancel
  state get [--chunk-chars N] / check --context VERSION
  config show / set --file JSON --context VERSION --actor NAME --request ID --by USER --basis TEXT
  upgrade --preview / --expect-source HASH --actor NAME --request ID --by USER --basis TEXT
具体子命令追加 --help；旧 task_queue.py 入口执行相同检查。''')
    else:
        sys.exit(main())
