# 中亚合作项目承诺协调

承接中亚经贸对接后的主体核验、资源暂留和项目履约。

## 领域范围

系统涉及辽宁企业、中亚合作企业、商协会秘书处、地方项目服务人员。当前领域资料记录以下业务事实：

- 合作参与者来自多个中亚国家的机构和企业
- 对接方向包含本地化生产、跨境物流和陆港共建
- 农业种植与精深加工需要联合投资

后续实现需要围绕这些边界组织服务：

- 跨境主体版本
- 需求供给解释
- 组合资源暂留
- 尽调授权
- 履约到期恢复

`contracts/domain.schema.json` 定义领域资料结构，`fixtures/domain.json` 提供不含真实身份信息的示例，`src/central_asia_project_commitment/context.py` 负责读取并检查这些资料。

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

上述命令只读取仓库内资料，不需要连接外部业务服务。
