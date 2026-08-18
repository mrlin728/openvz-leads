OpenVZ Leads —— 安装前请读

它还需要 Claude Code CLI
  那是它的大脑，跑在你已有的 Claude 订阅上，没有额外的模型费用。
  从 https://claude.ai/download 安装后，在命令行执行一次：claude login
  应用打开后的第一屏会检查这一项。

关于 SmartScreen 警告
  这个安装包没有代码签名证书，所以 Windows 会提示"已保护你的电脑"。
  点"详细信息" → "仍要运行"即可。这是 Windows 对所有未签名程序的默认
  行为，和程序本身无关。

装完之后
  打开后浏览器会自动弹出仪表盘（http://127.0.0.1:5555）。
  你的数据全部留在本机：%APPDATA%\OpenVZ Leads\
  提示词和销售知识库在那个目录的 prompts\ 和 skills\ 里，都是纯 Markdown，
  想改它怎么卖，直接改文件，不用碰代码。
  卸载不会删除这个目录 —— 你的客户名单和分析都在里面。

默认一封信都不会发出去
  它只负责找客户、分析客户、把开发信写好，然后等你在"待审核"里过目。

MIT 协议。衍生自 Ethan Rogers 的 Harvey。
https://www.openvzai.com/leads
