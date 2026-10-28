# 生成脚本ONES/update_bug.py， 修改ONES中某一个或几个issue的某些属性信息

## 参考命令
```shell
python update_bug.py --bug  FUCO-7 FUCO-4 --leixing "安全问题" --chengdu "致命" --lingling "fusion2026.1.100.01" --force  --yes
# 修改FUCO-7 FUCO-4的相关信息
# leixing即缺陷类型
# chengdu即严重程度
# lingling即ling-build-version
```

## 要求
- 缺陷类型 和严重程度 在ONES系统内都是单选的，修改前要检查参数是不是在单选列表里面， 所以建议第一次运行把合法的列表存储到本地， 只有在"--force"时才再次生成， 或者说仅当force时才会全局创建单选和法列表的json文件
- 两个环境变量ONES_BUG和ONES_API定义ONES BUG地址和API key
- ONES_BUG示例：https://ones.cn/project/#/team/KKv1z9x9/project/FUCO/component/6zAPyNY5/nav/Ur8qw1B82iFXfYVV/view/N1D6osqb/issue/FUCO-7
- 程序自动提取ONES URL  team id ，project id等信息
- 默认不直接修改，只有--yes才会真的修改
- 用单一脚本实现上述功能（不做import的动作）
