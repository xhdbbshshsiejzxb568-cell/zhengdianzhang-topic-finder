# 房产选题助手：真实采样执行协议（通用版）

> **怎么用**：把本文件复制一份，改名为 `SAMPLER.md`（只留在你自己的电脑上）。程序派单时，会让 AI 先读 `SAMPLER.md` 再采样。
> 下面的种子词以重庆为例。换成你的城市时，把「重庆」相关的种子词和分组名称一起改掉。

此目录是长期本地程序。程序不抓取平台API，不持有账号凭据。只通过当前 Codex 桌面任务实际可用、已授权的浏览器工具读 Chrome 真实可见页面。普通 codex exec 没有自动获得桌面浏览器能力。Codex 命令行需要支持 `codex queue --thread --message`（较新版本支持）；页面按钮把请求排队送回 config.json 指定的当前任务。

## 请求生命周期

本文件所有 `app.py`、`launch.py` 命令的工作目录必须显式设为 本程序所在的目录（下文称「程序目录」）；或者使用该目录下脚本的完整路径。当前 Codex 任务可能在其他目录，不能照抄相对路径后假设运行成功。

收到带 request_id 的按钮排队消息，按以下顺序处理：

1. 读取本协议、`config.json`、指定的 `data/requests/REQUEST_ID.json`，并执行 `/usr/bin/python3 app.py state` 查看权威状态；请求文件是镜像，状态不一致以 state 命令为准。
2. 从当前运行环境的 `CODEX_THREAD_ID` 获取实际执行任务ID，并核对它与 `config.json.target_thread`、请求的 `target_thread` 完全一致。不能仅将配置中的ID填入参数、冒充当前任务身份；子任务、独立CLI或新任务不能领取本任务的采样。
3. 只有请求状态为 `queued`、未过期且三方ID一致，执行 `/usr/bin/python3 app.py claim REQUEST_ID --thread ACTUAL_THREAD_ID`。看到成功返回 `running` 后才能打开平台采样。claim 失败立即停止，不通过编辑JSON重置。
4. 若状态为 `running`，说明已经被领取；新的排队消息或定时唤醒不得再次采样。若是 `completed/partial/blocked/cancelled/expired`，直接结束。本轮不创建替代请求、不重放历史结果。静默处理没有新变化的重复消息。

按钮只做 `codex queue --thread ... --message ...` 派单，不会启动 `codex exec` 采样。派单成功仅代表消息进入目标任务队列，页面显示“排队”直到该任务实际处理并领取；目标任务正忙时需要等待。请求从创建起有效90分钟，排队延误不会延长有效期。

每天09:00的当前任务定时唤醒，先核对上述实际任务ID，再用北京时间当前日期执行 `/usr/bin/python3 app.py schedule-request --date YYYY-MM-DD`。此命令只创建当天幂等请求，不会向自己再次排队；必须直接读取返回的 `request.id/status/reused` 并执行上述领取流程。领取后先检查本轮浏览器工具是否可用，不可用就将该请求结束为blocked并说明原因。若返回了已有请求，仅在其仍为 `queued` 且未过期时领取；`running`或终态不得重采。同日已完成、停止或失败的定时请求不会自动重建。

采样需要本地回传页时，先读取 `http://127.0.0.1:18765/api/state` 检查服务；如服务未运行，在明确工作目录执行 `/usr/bin/python3 launch.py`，由已有启动器启动服务并打开外部Chrome。不要另建HTTP服务、另开端口或终止未知进程。启动器只负责页面与服务，不创建采样请求。浏览器工具缺失、登录或授权失败时，将本轮记为 `blocked` 并说明需用户处理的具体步骤，禁止以普通CLI、Playwright自建浏览器或平台接口替代采样。

每个新视频之前、每次滚动批次之前查看请求状态；已停止/过期立即停止浏览器操作。每读完一个视频即保存部分结果，并执行 `app.py progress REQUEST_ID '真实进度'`。任务有效期90分钟。若已到期，不要重新开始同一请求；已保存部分结果由程序保留。

## 采样

四组独立：cq_douyin 重庆抖音；cq_xhs 重庆小红书；cn_douyin 全国抖音；cn_xhs 全国小红书。目标各3个不同视频、尽量不同发布者，每视频约20条有效评论。按明确地域内容归组，搜索词或作者IP不能单独证明视频房源在重庆。重庆种子：重庆买房、重庆改善、重庆二手房、重庆四代住宅。全国种子：买房避坑、新房还是二手房、物业、交付、通勤。可基于已见评论有针对性扩展，但种子不是已验证关键词。

必须实际观察播放器、播放控件或时长。小红书图文不算。视频点赞>=1000或评论总数>=100，至少一项有可读证据，记录显示原值；1.2万应 value=12000 raw='1.2万' approximate=true。数字未知null，“赞”不是0。互动为观察时累计，不是近期增长。

优先近30天；原始日期与依据保存。仅月日、无年份时保守date=null，不计近期；不得用编辑日期冒充发布日期。昨天可按北京时间观察日换算并保存明确换算依据；“n天前”若是经过时间的约数，也保守覆盖n至n+1天所涉及日期。几周前保留 `date=null`，在 `date_range:{start,end,basis}` 记录完整可能日期区间，例如3周前按观察日减28天至减21天；只有整个区间都落在近30天才计近期，4周前减35天至减28天不能通过。区间必须包含边界日期：观察时间为9月14日13:41时，“1周前”用8月31日至9月7日，不能排除8月31日13:41之后；“n小时前”也覆盖n至n+1小时所涉及的全部日期。不要把区间端点写成精确发布日期。未经证实的区间精度也必须写明不确定，不计近期。未显示日期null。登录、验证码、权限拒绝或持续故障立即停对应平台、保存原因并交接；不绕过，不修改账号、安全设置，不点赞/评论/私信。

保存真实评论原文和DOM公开的评论ID。禁止从隐藏state、源码数据对象、接口、搜索摘要或AI总结补评论。没有ID填null，不能猜或把用户名/序号当评论ID。DOM包装ID必须证明确系稳定评论身份，否则null。图像表情无法文本化时保留文字部分并记录emoji_images/text_note，不把丢失表情解释为原始没有表情。作者回复仅明确作者标记/明确对应身份；机器人或平台AI仅明确标识；广告、纯表情保留排除原因。不凭文风猜测。

记录 query、filters、sort（评论排序未标明写默认/未标明，不能假称热度排序）、scrolls（滚动调用次数及页数可扩展）、read_count、观察时间。非随机样本、可见评论可能由平台排序且不代表全部评论。

`observed_at` 只填真实浏览器观察时间；不知道就null。程序生成的 `ingested_at/first_seen/last_seen` 是入库时间，不是浏览器观察时间。同一平台视频固定属于首次保存的一个组，跨组再次提交会被拒绝；请换其他视频，不能改ID规避。抖音公开URL中的 `modal_id`、`/video/ID`，以及小红书 `/search_result/24位ID`、`/explore/24位ID` 可直接用于规范化视频身份；没有公开ID仍填null。

## 结果与语义

payload 结构：`{batch_id:'每批唯一ID',videos:[{platform:'xhs|douyin',group:'cq_xhs|cq_douyin|cn_xhs|cn_douyin',video_id,url,title,author,observed_at,observed_at_basis,selection_note,observed_url,likes:{value,raw,approximate},comment_count:{value,raw,approximate},date_raw,published_date,published_date_basis,video_evidence:{type:'duration|player|controls',detail,duration_seconds},search:{query,filters,sort,scrolls,read_count},comments:[{comment_id,text,author,likes:{value,raw,approximate},date_raw,date,date_basis,date_range:{start,end,basis},emoji_images,text_note,comment_id_basis,is_author,is_bot,is_platform_ai,excluded_reason}]}],semantic_groups:[{group,label,comment_refs:[{platform,video_id,comment_id,quote}],topic:{title,angle,outline:[...],comment_refs:[{platform,video_id,comment_id,quote}]}}],platform_status:{platform:{status,reason}}}`。没有数据的对象和字段填null，不用空示例或0占位。

可执行 `app.py ingest REQUEST_ID FILE.json`。写文件要先同目录临时文件再os.replace。浏览器DOM原文可通过本地 http://127.0.0.1:18765/handoff.html 的表单填写请求ID与完整JSON，点击校验保存；此表单调用同一原子入库函数。仅当前有效且已领取请求允许入库。同batch_id重传不增加读取数。复查真实评论必须新batch_id，程序报告为重复，不能当新增。不要重新载入旧payload冒充新采样。

输入payload原样作为JSON对象原子归档到 `data/raw/请求ID/安全批次文件名.json`；原始归档不作为计数依据，可能包含未通过校验的字段。相同batch_id重传仅补缺少的原始归档、返回reused，不改读取/新增/重复数，也不覆盖已存在原始批次；修正已经保存的语义时用新的仅含semantic_groups的批次，不重新提交videos假装复查。

Codex必须逐条读原文，提出语义同义组及明确comment_refs、quote精确等于保存文本。程序仅核验这些ID/原文并计数，不能因某个词碰巧出现而自动扩充。一个评论可支持不同真实维度，但各词组内只算1条。作者/无ID/无可靠近期日期不会用来充排名。每组至少3个合格视频、30条近期有效独立评论才开放该组排名；词本身还须至少2个视频的近期评论支持。否则探索线索，禁止称平台热榜、搜索量或所有买房人需求。

每个选题给中文标题、切入角度、简短提纲，引用支持该角度的真实评论及视频链接。评论里房价、学校、政策、产权、交易和个人经历均是用户说法、待核实，不能当事实论据。证据不足可写提出问题的探索选题，不能补写未经验证结论。

具体选题宜提供 `topic.comment_refs` 精挑子集：必须属于本语义组，并再次逐ID、原文校验；仅显示通过的选题引用，不自动扩成词组全部成员。显式空列表或全部不合法则不生成该选题并报错。省略该字段才兼容沿用词组的全部有效引用，因此只有确实每条都支持具体选题时才省略。

确认返回已保存且检查校验错误后，再执行 `app.py finish REQUEST_ID --status completed|partial|blocked --reason '实际缺口'`。completed只代表本轮结束，不代表四组均达排名门槛；不足优先partial。若请求已经被用户取消或已到期，finish会拒绝，不能恢复或覆盖终态。

权威数据为 `data/state.json`，请求镜像为 `data/requests/请求ID.json`，日报历史为 `data/reports/请求ID.json`，服务日志为 `data/server.log`。原子提交先保存权威state，再落日报镜像；若磁盘问题使镜像暂未写出，已提交快照仍可通过历史API读取。禁止手改这些文件补数量。必须报告本轮read/new/duplicates、累计独立、各组视频有效评论近期评论、排名关键词/探索线索/选题数量，不能把历史累计算今天新增。

## 定时条件与通知

首次真实运行验收后，通过本机 Codex 桌面 `automation_update` 创建或更新附着当前任务的 heartbeat，每天北京时间09:00。先检查已有同名/同用途任务避免重复，目标任务须与config一致。不能换成每次新建任务的独立cron，也不能把普通CLI调用写成浏览器执行器。具体是否已启用，以Codex定时任务实际保存结果和界面为准；本文件不是启用证明。

Mac开机且唤醒，Codex运行且已登录，Chrome及其浏览器扩展授权有效，各平台登录和网络可用，程序目录仍在原位。不会在关机后本地采样；不要承诺休眠期间准点运行。只在首次新增评论/新选题等新结果、发生失败或需要用户操作时通知；相同阻塞原因未变化、无新增、重复队列和已完成请求保持安静。新选题需与已有日报对照，不能把同一选题每日重建算作新结果。比较本轮与上次日报的数量、选题和平台原因后再决定是否通知，通知报告实际新增和重复，不只说“任务执行了”。

官方能力说明经2026-09-14核验：[OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)。官方说明支持在现有任务中按日唤醒；本地文件任务需要电脑开机、桌面应用运行。浏览器是否可用仍必须每轮实查，不能由定时功能支持反推。
