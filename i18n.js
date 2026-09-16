(() => {
    'use strict';

    const STORAGE_KEY = 'agy_usage_tracker_language';
    const SUPPORTED = new Set(['vi', 'en']);
    const originalText = new WeakMap();
    const originalAttrs = new WeakMap();
    let language = 'vi';
    let applying = false;

    const EN = [
        ['Language / Ngôn ngữ:', 'Language:'],
        ['Tiếng Việt', 'Vietnamese'],
        ['Đang tải...', 'Loading...'],
        ['Đang tải…', 'Loading…'],
        ['Đang xây dựng ma trận…', 'Building matrix…'],
        ['Đang đọc lịch sử Codex…', 'Reading Codex history…'],
        ['Đang xây dựng mức so sánh cho từng model...', 'Building a baseline for each model...'],
        ['Đang tính toán...', 'Calculating...'],
        ['Đang kiểm tra nguồn dữ liệu', 'Checking data source'],
        ['Nhỏ', 'Compact'],
        ['Lớn', 'Spacious'],
        ['Phiên Đáng Kiểm Tra', 'Sessions Worth Reviewing'],
        ['Mã Phiên', 'Session ID'],
        ['Chi Phí', 'Cost'],
        ['Thời Lượng', 'Duration'],
        ['Bắt Đầu', 'Started'],
        ['Bắt đầu', 'Started'],
        ['Thao Tác', 'Actions'],
        ['Toàn Bộ Nhật Ký Phiên Làm Việc', 'All Session Logs'],
        ['Tìm những phiên dùng token, chi phí, thời gian hoặc tool calls cao bất thường so với các phiên cùng model. Mở từng phiên để xem lại các bước và xác định nguyên nhân tiêu hao.', 'Find sessions with unusually high token use, cost, duration, or tool calls relative to sessions using the same model. Open a session to inspect its steps and trace the source of the extra usage.'],
        ['Tìm những phiên dùng token, chi phí, thời gian hoặc tool calls cao bất thường so với các phiên cùng model.', 'Find sessions with unusually high token use, cost, duration, or tool calls relative to sessions using the same model.'],
        ['Mở từng phiên để xem lại các bước và xác định nguyên nhân tiêu hao.', 'Open a session to inspect its steps and trace the source of the extra usage.'],
        ['Lọc theo tín hiệu, model hoặc từ khóa rồi nhấp vào hàng để xem chi tiết từng bước.', 'Filter by signal, model, or keyword, then click a row to inspect the session step by step.'],
        ['Tất cả tín hiệu', 'All signals'],
        ['Cần kiểm tra', 'Needs review'],
        ['Nhiều tool calls', 'Many tool calls'],
        ['Thời lượng cao', 'Long duration'],
        ['Bất thường trước', 'Anomalies first'],
        ['Mới nhất trước', 'Newest first'],
        ['Cũ nhất trước', 'Oldest first'],
        ['Nhiều tokens nhất', 'Most tokens'],
        ['Nhiều tool calls nhất', 'Most tool calls'],
        ['Chi phí cao nhất', 'Highest cost'],
        ['Nguồn', 'Source'],
        ['Yêu Cầu Ban Đầu (Prompt)', 'Initial Request (Prompt)'],
        ['Model Sử Dụng', 'Model Used'],
        ['Thời Gian Bắt Đầu', 'Start Time'],
        ['Tin Nhắn', 'Messages'],
        ['Phản Hồi', 'Responses'],
        ['Ước Tính Phí', 'Estimated Cost'],
        ['Theo dõi chính xác mức tiêu tốn và', 'Track actual consumption and'],
        ['hạn mức còn lại', 'remaining quota'],
        ['theo chu kỳ', 'across'],
        ['5 Giờ', '5 Hours'],
        ['và', 'and'],
        ['Theo Tuần (7 Ngày)', 'Weekly (7 Days)'],
        [', tách biệt giữa', ', separated between'],
        ['tách biệt giữa', 'separated between'],
        ['Model Ngoài', 'External Models'],
        [', đồng thời quản lý các tài khoản Gmail đã đăng nhập.', ', while also keeping track of signed-in Gmail accounts.'],
        ['ĐỒNG BỘ THÔNG SỐ CHUẨN (IDE SETTINGS)', 'SYNC REFERENCE VALUES (IDE SETTINGS)'],
        ['Hiển thị song song', 'Show side by side'],
        ['chỉ số chính xác từ IDE Settings', 'exact values from IDE Settings'],
        ['ước lượng tiêu thụ chi tiết theo token / lượt prompt', 'detailed consumption estimates by token / prompt turn'],
        ['từ nhật ký cục bộ.', 'from local logs.'],
        ['Khung Hạn Mức 5 Giờ (5-Hour Rolling Window)', '5-Hour Quota Window (Rolling Window)'],
        ['Hạn mức hồi phục liên tục theo thời gian trôi qua. Tự động tính toán số tokens & lượt tương tác trong 5 giờ gần nhất.', 'Quota recovers continuously as time passes. The tracker automatically calculates tokens and interactions in the latest five-hour window.'],
        ['Mốc giờ hồi 100%:', 'Estimated full-recovery time:'],
        ['🔄 Vừa Reset Chu Kỳ 5H (Bắt Đầu Đếm Mới)', '🔄 5H Cycle Just Reset (Start a New Count)'],
        ['MODEL NGOÀI', 'EXTERNAL MODELS'],
        ['Model Gemini (Tuần)', 'Gemini Models (Weekly)'],
        ['Chu Kỳ 7 Ngày', '7-Day Cycle'],
        ['Hạn Mức Còn Lại (Tuần)', 'Quota Remaining (Weekly)'],
        ['Đã Dùng (7 Ngày)', 'Used (7 Days)'],
        ['Còn Lại (Tuần)', 'Remaining (Weekly)'],
        ['Giới Hạn Tuần', 'Weekly Limit'],
        ['Lượt Request', 'Requests'],
        ['Mốc reset tuần (100%):', 'Weekly reset time (100%):'],
        ['Claude & External (Tuần)', 'Claude & External (Weekly)'],
        ['Hạn Mức Codex Thời Gian Thực (Codex Rate Limits)', 'Live Codex Quota (Codex Rate Limits)'],
        ['Cập nhật hạn mức và mốc reset trực tiếp từ Codex. Bản ghi phiên làm việc được dùng dự phòng khi không kết nối được.', 'Read quota and reset times directly from Codex. Session logs are used as a fallback when the live connection is unavailable.'],
        ['Lịch Sử Hạn Mức & Biến Động Theo Thời Gian', 'Quota History & Changes Over Time'],
        ['Theo dõi khung 5 giờ, khung tuần, quá trình hội tụ trần hạn mức và các lần thay đổi chính sách được phát hiện.', 'Track the 5-hour window, weekly window, quota-capacity convergence, and detected provider policy changes.'],
        ['⏱️ Khung 5 Giờ', '⏱️ 5-Hour Window'],
        ['📅 Khung 1 Tuần', '📅 1-Week Window'],
        ['📊 Biến Thiên Tổng Hạn Mức (Max Capacity)', '📊 Quota Capacity Changes (Max Capacity)'],
        ['📋 Nhật Ký Điểm Đo', '📋 Measurement Log'],
        ['Biểu Đồ Sóng Tiêu Thụ Khung 5 Giờ (15 Phút/Điểm)', '5-Hour Consumption Waveform (15 Minutes/Point)'],
        ['Đường diện tích dạng sóng phản ánh biến thiên token và vạch trần hạn mức Pro', 'The area waveform shows token consumption over time together with the observed Pro quota ceiling'],
        ['Model Ngoài (5h)', 'External Models (5h)'],
        ['Vạch Hạn Mức Giới Hạn', 'Quota Limit Line'],
        ['Biểu Đồ Cột & Xu Hướng Tiêu Thụ Khung 1 Tuần (7 Ngày Qua)', 'Weekly Consumption Bars & Trend (Last 7 Days)'],
        ['Phân bổ lượng token theo từng ngày trong tuần giữa Gemini và Model Ngoài', 'Daily token distribution across Gemini and external models during the week'],
        ['Gemini Hàng Ngày', 'Gemini Daily'],
        ['Model Ngoài Hàng Ngày', 'External Models Daily'],
        ['Vạch Hạn Mức Tuần', 'Weekly Quota Line'],
        ['Biểu Đồ Hội Tụ Trần Dung Lượng Tối Đa (Max Capacity Evolution)', 'Maximum Capacity Convergence (Max Capacity Evolution)'],
        ['Mỗi điểm là một lần calibration thực tế; đường recent-cycle giúp phát hiện sớm khi trần mới lệch khỏi bộ hội tụ dài hạn', 'Each point is a real calibration observation; the recent-cycle line helps detect when a new ceiling diverges from the long-term convergence range'],
        ['Đang tải evidence/confidence...', 'Loading evidence/confidence...'],
        ['Transcript và exact worker được giữ riêng; mixed capacity chỉ là bằng chứng phụ thuộc source mix.', 'Transcript and exact-worker evidence are kept separate; mixed capacity is treated only as source-mix-dependent evidence.'],
        ['Bằng Chứng & Lịch Sử Biến Động Hạn Mức Nhà Cung Cấp (Policy Shift Audit Log)', 'Provider Quota Evidence & Change History (Policy Shift Audit Log)'],
        ['Cảnh báo khi thay đổi ≥15% được xác nhận bởi ≥2 điểm recent-cycle độc lập hoặc ≥2 calibration points hội tụ', 'Alert when a ≥15% change is confirmed by ≥2 independent recent-cycle points or ≥2 converged calibration points'],
        ['Mốc Phát Hiện', 'Detected At'],
        ['Nhà Cung Cấp', 'Provider'],
        ['Hạn Mức', 'Quota'],
        ['Loại Biến Động', 'Change Type'],
        ['Dung Lượng Cũ → Mới', 'Old → New Capacity'],
        ['Ghi Chú Bằng Chứng', 'Evidence Notes'],
        ['Nhật Ký Điểm Đo Snapshot Thời Gian Thực', 'Live Snapshot Measurement Log'],
        ['Tự động lấy mẫu snapshot mỗi khi có tác vụ mới', 'Automatically capture a snapshot when new work is detected'],
        ['Mốc Thời Gian', 'Timestamp'],
        ['Model Đang Chạy', 'Active Model'],
        ['Token Đã Dùng 5H', 'Tokens Used (5H)'],
        ['Token Đã Dùng Tuần', 'Tokens Used (Weekly)'],
        ['% Còn Lại Tuần', '% Remaining (Weekly)'],
        ['% Còn Lại 5H', '% Remaining (5H)'],
        ['Trạng Thái', 'Status'],
        ['Danh Sách Các Tài Khoản Gmail Đã Đăng Nhập', 'Signed-in Gmail Accounts'],
        ['Hệ thống tự động ghi nhớ và lưu giữ danh mục các tài khoản Gmail từng sử dụng kèm hạn mức riêng biệt.', 'The tracker remembers locally used Gmail accounts and keeps their quota views separate.'],
        ['+ Thêm Tài Khoản Gmail', '+ Add Gmail Account'],
        ['Tài Khoản Gmail', 'Gmail Account'],
        ['Họ & Tên', 'Full Name'],
        ['Gói Tài Khoản', 'Account Plan'],
        ['Hạn Mức 5h (Gemini / Ngoài)', '5h Quota (Gemini / External)'],
        ['Hạn Mức Tuần (Gemini / Ngoài)', 'Weekly Quota (Gemini / External)'],
        ['Lần Hoạt Động Gần Nhất', 'Last Active'],
        ['⚡ ARTIFICIAL ANALYSIS INTELLIGENCE INDEX v4.3 • CẬP NHẬT 09/09/2026', '⚡ ARTIFICIAL ANALYSIS INTELLIGENCE INDEX v4.3 • UPDATED 09/09/2026'],
        ['Bảng Xếp Hạng Models — Chọn Model Theo Việc Cần Làm', 'Model Leaderboard — Choose a Model for the Job'],
        ['Điểm trí tuệ, tốc độ và chi phí/task lấy từ', 'Intelligence, speed, and cost/task come from'],
        ['; nhãn “ước tính” được giữ nguyên khi AA chưa hoàn tất đo độc lập. Mỗi dòng còn ghép với', '; the “estimated” label is preserved when AA has not completed an independent measurement. Each row is also combined with'],
        ['; nhãn “ước tính” được giữ nguyên khi AA chưa hoàn tất đo độc lập.', '; the “estimated” label is preserved when AA has not completed an independent measurement.'],
        ['Mỗi dòng còn ghép với', 'Each row is also combined with'],
        ['token, chi phí all-time và quota/task đo từ log cục bộ', 'local-log token totals, all-time cost, and quota/task measurements'],
        ['để hỗ trợ quyết định thực tế.', 'to support real-world model decisions.'],
        ['So Sánh Intelligence Index v4.3 Và Tốc Độ Output', 'Intelligence Index v4.3 vs Output Speed'],
        ['Tốc Độ Sinh Token (Output Tokens/giây)', 'Token Generation Speed (Output Tokens/sec)'],
        ['Bảng Quyết Định Model', 'Model Decision Table'],
        ['Mặc định chỉ hiện sáu lựa chọn có ích nhất. Mở “Tất cả AA v4.3” để xem các biến thể hiện tại và lịch sử.', 'By default, only the six most useful choices are shown. Open “All AA v4.3” to inspect current and historical variants.'],
        ['Phạm vi: Khuyến nghị', 'Scope: Recommended'],
        ['Phạm vi: Thế hệ hiện tại', 'Scope: Current generation'],
        ['Phạm vi: Đã dùng cục bộ', 'Scope: Used locally'],
        ['Phạm vi: Tất cả AA v4.3', 'Scope: All AA v4.3'],
        ['Họ model: Tất cả', 'Model family: All'],
        ['Xếp theo: Khuyến nghị', 'Sort by: Recommendation'],
        ['Xếp theo: Intelligence cao nhất', 'Sort by: Highest intelligence'],
        ['Xếp theo: Tốc độ cao nhất', 'Sort by: Highest speed'],
        ['Xếp theo: IQ/$task cao nhất', 'Sort by: Highest IQ/$task'],
        ['Xếp theo: AA cost/task thấp nhất', 'Sort by: Lowest AA cost/task'],
        ['Xếp theo: Quota/task thấp nhất', 'Sort by: Lowest quota/task'],
        ['Xếp theo: Token cục bộ nhiều nhất', 'Sort by: Most local tokens'],
        ['Hạng AA', 'AA Rank'],
        ['Model, nguồn & trạng thái', 'Model, Source & Status'],
        ['Tốc độ', 'Speed'],
        ['Quota cục bộ', 'Local Quota'],
        ['All-time cục bộ', 'Local All-Time'],
        ['Quyết định sử dụng', 'Usage Decision'],
        ['Khuyến Nghị Chọn Model Cho Từng Nhu Cầu (Model Recommender)', 'Model Recommendations by Need (Model Recommender)'],
        ['Thống Kê Chi Tiết Từng Model & Mức Suy Luận', 'Detailed Statistics by Model & Reasoning Effort'],
        ['Mỗi tổ hợp', 'Each combination of'],
        ['tên model + mức suy luận', 'model name + reasoning effort'],
        ['(ví dụ: "Gemini 3.7 Flash High", "5.6 sol high", "o3 xhigh") được tính là 1 model riêng biệt. Dữ liệu Antigravity và Codex được tự động quét từ log cục bộ real-time; dữ liệu nhập thủ công chỉ dùng làm nguồn dự phòng (Manual fallback).', '(for example, "Gemini 3.7 Flash High", "5.6 sol high", or "o3 xhigh") is treated as a separate model. Antigravity and Codex data are scanned automatically from local logs in real time; manually entered data is used only as a fallback.'],
        ['(ví dụ: "Gemini 3.7 Flash High", "5.6 sol high", "o3 xhigh") được tính là 1 model riêng biệt.', '(for example, "Gemini 3.7 Flash High", "5.6 sol high", or "o3 xhigh") is treated as a separate model.'],
        ['Dữ liệu Antigravity và Codex được tự động quét từ log cục bộ real-time; dữ liệu nhập thủ công chỉ dùng làm nguồn dự phòng (Manual fallback).', 'Antigravity and Codex data are scanned automatically from local logs in real time; manually entered data is used only as a fallback.'],
        ['Bảng Phân Tích Theo Từng Model', 'Per-Model Analysis Table'],
        ['+ Thêm Model Codex', '+ Add Codex Model'],
        ['Cài Hạn Mức Codex', 'Set Codex Quota'],
        ['Biểu Đồ Xu Hướng Tiêu Thụ Tokens Theo Model', 'Token Consumption Trend by Model'],
        ['(7 Ngày Qua)', '(Last 7 Days)'],
        ['Cột chồng thể hiện tổng tokens theo đơn vị thời gian đã chọn; từng phần màu cho biết lượng tokens của từng model.', 'Stacked bars show total tokens for the selected time unit; each colored segment represents one model.'],
        ['Khoảng thời gian biểu đồ model', 'Model chart date range'],
        ['Đơn vị thời gian biểu đồ model', 'Model chart time unit'],
        ['Ngày bắt đầu', 'Start date'],
        ['Ngày kết thúc', 'End date'],
        ['Dữ liệu biểu đồ token theo thời gian, cuộn ngang', 'Token timeline chart data, horizontally scrollable'],
        ['1 năm qua', 'Past year'],
        ['Tùy chọn', 'Custom'],
        ['Hiển thị theo', 'Group by'],
        ['Ngày', 'Day'],
        ['Tháng', 'Month'],
        ['Năm', 'Year'],
        ['đến', 'to'],
        ['Kéo thanh ngang để xem các mốc thời gian cũ hơn; biểu đồ mở ở mốc mới nhất.', 'Drag the horizontal scrollbar to inspect older periods; the chart opens at the newest point.'],
        ['Biểu Đồ Xu Hướng Chi Phí Theo Model', 'Estimated Cost Trend by Model'],
        ['Cột chồng thể hiện chi phí API ước tính và dùng cùng khoảng thời gian cùng đơn vị Ngày / Tháng / Năm với biểu đồ tokens phía trên.', 'Stacked bars show estimated API cost using the same date range and Day / Month / Year grouping as the token chart above.'],
        ['Đơn vị', 'Unit'],
        ['Đơn vị biểu đồ chi phí model', 'Model cost chart unit'],
        ['Dữ liệu biểu đồ chi phí theo thời gian, cuộn ngang', 'Cost timeline chart data, horizontally scrollable'],
        ['Credit chuẩn hóa', 'Normalized credits'],
        ['Credit chuẩn hóa là đơn vị nội bộ cố định:', 'A normalized credit is a fixed internal unit:'],
        ['1 credit = $0.01 chi phí API ước tính', '1 credit = $0.01 estimated API cost'],
        ['. Đây không phải credit thanh toán chính thức của OpenAI.', '. This is not an official OpenAI billing credit.'],
        ['Median trượt cục bộ; mỗi điểm so model với Sol High trong cùng cửa sổ thời gian. Đây là số đo thực nghiệm từ log, không phải trọng số chính thức của OpenAI.', 'Local rolling median; each point compares a model with Sol High in the same time window. This is an empirical measurement from local logs, not an official OpenAI weighting.'],
        ['Phạm vi biểu đồ hiệu suất hạn mức', 'Quota-efficiency chart range'],
        ['Cửa sổ median trượt', 'Rolling median window'],
        ['Phạm vi biểu đồ tiêu hao hạn mức theo task', 'Quota-per-task chart range'],
        ['Cửa sổ median trượt theo task', 'Rolling median window for quota per task'],
        ['1 ngày · nhạy', '1 day · responsive'],
        ['7 ngày · nhạy', '7 days · responsive'],
        ['7 ngày · ổn định', '7 days · stable'],
        ['14 ngày · cân bằng', '14 days · balanced'],
        ['3 ngày · cân bằng', '3 days · balanced'],
        ['30 ngày · ổn định', '30 days · stable'],
        ['7 ngày qua', 'Last 7 days'],
        ['30 ngày qua', 'Last 30 days'],
        ['90 ngày qua', 'Last 90 days'],
        ['180 ngày qua', 'Last 180 days'],
        ['Cửa Sổ Trước', 'Previous Window'],
        ['Biến Động', 'Change'],
        ['Mẫu Model / Sol', 'Model / Sol Samples'],
        ['Ghi Nhận Gần Nhất', 'Latest Observation'],
        ['Hệ số gần nhất so với Sol High trong cùng cửa sổ. Lớn hơn 1× nghĩa là đốt quota nhanh hơn trên cùng raw token.', 'Latest ratio vs Sol High in the same window. Greater than 1× means quota is consumed faster for the same raw token.'],
        ['Lớn hơn 1× nghĩa là task gần đây tiêu hao quota nhiều hơn Sol High trong cùng cửa sổ.', '>1× means recent tasks consumed more quota than Sol High in the same window.'],
        ['Lớn hơn 1× nghĩa là tiêu hao hạn mức nhanh hơn Sol High trên cùng số raw token.', 'Greater than 1× means quota is consumed faster than Sol High for the same raw token count.'],
        ['Lớn hơn 1× nghĩa là ước tính tiêu hao hạn mức nhiều hơn Sol High cho một task.', 'Greater than 1× means the estimated quota consumption per task is higher than Sol High.'],
        ['Chưa đủ dữ liệu cục bộ có Sol High cùng thời điểm để so sánh.', 'Insufficient local data with Sol High in the same time period for comparison.'],
        ['Median trượt của các task hoàn tất; mỗi điểm so model với Sol High trong cùng cửa sổ và chỉ hiện khi mỗi bên có ít nhất 5 task hợp lệ.', 'Rolling median of completed tasks; each point compares a model with Sol High in the same window and is shown only when each side has at least five valid tasks.'],
        ['Gần Nhất %5h / Task', 'Latest %5h / Task'],
        ['Coverage / Tin Cậy', 'Coverage / Confidence'],
        ['Chưa đủ ít nhất 5 task của model và Sol High trong cùng cửa sổ để so sánh.', 'Fewer than five valid tasks are available for both the model and Sol High in the same window.'],
        ['Quota Span / Phiên', 'Quota Span / Session'],
        ['Mẫu', 'Samples'],
        ['Chưa đủ dữ liệu để ước tính hiệu suất hạn mức Codex 5h.', 'Insufficient data to estimate Codex 5h quota efficiency.'],
        ['Ước %5h / Task', 'Estimated %5h / Task'],
        ['Quan Sát / Task', 'Observed / Task'],
        ['Task / Phiên', 'Tasks / Session'],
        ['Chưa đủ dữ liệu task hoàn tất để ước tính.', 'Insufficient completed-task data for an estimate.'],
        ['Model (Tên + Mức Suy Luận)', 'Model (Name + Reasoning Effort)'],
        ['Nền Tảng', 'Platform'],
        ['Hôm Nay · Tokens + $', 'Today · Tokens + $'],
        ['7 Ngày · Tokens + $', '7 Days · Tokens + $'],
        ['TB / Phản Hồi', 'Avg / Response'],
        ['Tokens và chi phí ước tính từ 00:00 hôm nay', 'Tokens and estimated cost since 00:00 today'],
        ['Tokens và chi phí ước tính trong 7 ngày gần nhất', 'Tokens and estimated cost over the last 7 days'],
        ['Tokens và chi phí ước tính tích lũy toàn thời gian', 'All-time cumulative tokens and estimated cost'],
        ['Trung bình tokens trên mỗi phản hồi, tách Input / Output / Thinking', 'Average tokens per response, split into Input / Output / Thinking'],
        ['Tỉ lệ % hạn mức tuần đã tiêu thụ (Weekly Tokens ÷ Hạn Mức Tuần)', 'Percentage of weekly quota used (Weekly Tokens ÷ Weekly Limit)'],
        ['Phiên', 'Sessions'],
        ['Ghép mọi lượt sửa, giảng lại và tiếp tục cho đến khi một mục tiêu được chấp nhận. Ma trận chỉ dùng nhiệm vụ một model và đủ mẫu; tuyến đổi model hoặc có subagent được giữ riêng để tránh quy công sai.', 'Group every correction, re-explanation, and continuation until one objective is accepted. The matrix uses only single-model missions with enough samples; model-switch and subagent routes are kept separate to avoid assigning credit to the wrong model.'],
        ['Ghép mọi lượt sửa, giảng lại và tiếp tục cho đến khi một mục tiêu được chấp nhận.', 'Group every correction, re-explanation, and continuation until one objective is accepted.'],
        ['Ma trận chỉ dùng nhiệm vụ một model và đủ mẫu; tuyến đổi model hoặc có subagent được giữ riêng để tránh quy công sai.', 'The matrix uses only single-model missions with enough samples; model-switch and subagent routes are kept separate to avoid assigning credit to the wrong model.'],
        ['Sol High là 1.00× trong cùng loại công việc. Ô thiếu mẫu không được nội suy.', 'Sol High is 1.00× within each task type. Sparse cells are never interpolated.'],
        ['90 ngày gần nhất', 'Last 90 days'],
        ['Đủ mẫu', 'Enough samples'],
        ['Hệ số <1× dùng ít token hơn Sol High; >1× dùng nhiều hơn.', 'A ratio <1× uses fewer tokens than Sol High; >1× uses more.'],
        ['Tự động phân nhóm chỉ là gợi ý. Hãy sửa loại việc, kết quả hoặc ranh giới trước khi tin một ô ít mẫu.', 'Automatic grouping is only a suggestion. Correct the task type, outcome, or mission boundary before trusting a sparse cell.'],
        ['Tất cả kết quả', 'All outcomes'],
        ['Mọi tuyến model', 'All model routes'],
        ['Một model', 'Single model'],
        ['Tuyến model', 'Model route'],
        ['Lượt / Sửa', 'Turns / Corrections'],
        ['Công dạy thêm', 'Extra guidance'],
        ['Tin cậy', 'Confidence'],
        ['Ranh giới', 'Boundary'],
        ['Nhập % còn lại trong IDE Settings để bộ ước lượng tự học và chính xác dần qua nhiều lần đo.', 'Enter the remaining percentage from IDE Settings so the estimator can learn and improve across multiple observations.'],
        ['token rolling đang theo dõi', 'rolling tokens currently tracked'],
        ['Gemini (5 Giờ) Còn Lại:', 'Gemini (5 Hours) Remaining:'],
        ['Claude / GPT (5 Giờ) Còn Lại:', 'Claude / GPT (5 Hours) Remaining:'],
        ['Gemini (Theo Tuần) Còn Lại:', 'Gemini (Weekly) Remaining:'],
        ['Claude / GPT (Theo Tuần) Còn Lại:', 'Claude / GPT (Weekly) Remaining:'],
        ['Gemini 5h còn (Số phút):', 'Gemini 5h remaining (Minutes):'],
        ['Model Ngoài 5h còn (Số phút):', 'External models 5h remaining (Minutes):'],
        ['Gemini Tuần còn (Số giờ):', 'Gemini weekly remaining (Hours):'],
        ['Model Ngoài Tuần còn (Số giờ):', 'External models weekly remaining (Hours):'],
        ['Lưu & Đồng Bộ Ngay', 'Save & Sync Now'],
        ['Google AI Ultra (Hạn mức cao)', 'Google AI Ultra (High quota)'],
        ['+ Thêm & Lưu Tài Khoản', '+ Add & Save Account'],
        ['Cộng dồn', 'Add to existing totals'],
        ['vào số liệu cũ (Phiên mới)', 'as a new session'],
        ['Ghi đè', 'Overwrite'],
        ['toàn bộ', 'all existing values'],
        ['💾 Lưu Model', '💾 Save Model'],
        ['AGY Live Usage Tracker — Theo Dõi & Phân Tích Chi Tiết', 'AGY Live Usage Tracker — Detailed Usage & Cost Analytics'],
        ['Giám sát & phân tích mức độ tiêu tốn Token, Chi phí & Tool Calls trong IDE', 'Monitor and investigate token usage, cost, and tool calls across local coding assistants'],
        ['Nhật Ký & Điều Tra Tiêu Hao', 'Usage Log & Investigation'],
        ['Nhật Ký & Điều Tra', 'Usage Log & Investigation'],
        ['Hạn Mức & Tài Khoản (5h / Tuần)', 'Quota & Accounts (5h / Weekly)'],
        ['Bảng Xếp Hạng Models (Artificial Analysis đã xác minh)', 'Model Leaderboard (verified Artificial Analysis data)'],
        ['Phân Tích Models & Chi Phí', 'Models & Cost Analysis'],
        ['Hiệu Quả Theo Công Việc', 'Task Outcome Efficiency'],
        ['Hiệu Quả Model Theo Loại Công Việc', 'Model Efficiency by Task Type'],
        ['Ma Trận Tổng Token Đến Khi Đạt Yêu Cầu', 'Total Tokens Until Acceptance Matrix'],
        ['Kiểm Tra Các Nhiệm Vụ Đã Ghép', 'Review Grouped Missions'],
        ['Hạn Mức Sử Dụng Còn Lại & Quản Lý Tài Khoản', 'Remaining Quota & Account Management'],
        ['Hạn Mức Thời Gian Thực & Định Lượng Token', 'Live Quota & Token Capacity'],
        ['Nguồn Dữ Liệu Gemini (Antigravity IDE, Antigravity CLI & Gemini CLI Official)', 'Gemini Data Sources (Antigravity IDE, Antigravity CLI & official Gemini CLI)'],
        ['Hạn mức Gemini bên dưới kết hợp tổng tokens từ', 'The Gemini quota below combines token totals from'],
        ['Khung Hạn Mức Theo Tuần (7-Day Weekly Window)', 'Weekly Quota Window (7 days)'],
        ['Tổng ngân sách và hạn mức tiêu thụ được reset / cộng dồn theo chu kỳ tuần.', 'Total budget and usage quota reset / accumulate on the weekly cycle.'],
        ['Biến Động Hiệu Suất Hạn Mức 5h Theo Thời Gian', '5h Quota Efficiency Over Time'],
        ['Biến Động Tiêu Hao Hạn Mức 5h Theo Task', '5h Quota Consumption by Task Over Time'],
        ['Hiệu Suất Hạn Mức Codex 5h', 'Codex 5h Quota Efficiency'],
        ['Tiêu Hao Hạn Mức Codex 5h Theo Task', 'Codex 5h Quota Consumption by Task'],
        ['Đo thực nghiệm từ snapshot rate-limit trong log Codex cục bộ; không phải công thức trọng số chính thức của OpenAI.', 'Empirical measurement from local Codex rate-limit snapshots; not an official OpenAI weighting formula.'],
        ['Đo theo task hoàn tất trong log Codex cục bộ; %5h/task được ngoại suy theo độ phủ token. Task đổi model hoặc đi qua mốc reset 5h được loại khỏi mẫu; đây không phải công thức chính thức của OpenAI.', 'Measured from completed tasks in local Codex logs; %5h/task is extrapolated from token coverage. Model-switching tasks and tasks crossing a 5h reset are excluded; this is not an official OpenAI formula.'],
        ['Hiệu Chỉnh Hạn Mức Quota (IDE Settings)', 'Calibrate Quota Limits (IDE Settings)'],
        ['Nguyên lý tự hiệu chuẩn:', 'How auto-calibration works:'],
        ['% Còn Lại', '% Remaining'],
        ['mỗi lần bạn nhập', 'each time you enter'],
        ['hệ thống lưu nó cùng snapshot', 'the system stores it with a snapshot of'],
        [', hệ thống lưu nó cùng snapshot', ', the system stores it with a snapshot of'],
        ['thành một điểm đo.', 'as one measurement point.'],
        ['Nhiều điểm đo sẽ được đối chiếu bằng estimator chống nhiễu/làm tròn để suy ra dung lượng quota.', 'Multiple measurements are reconciled by a noise/rounding-resistant estimator to infer quota capacity.'],
        ['Một lần nhập đơn lẻ chỉ là mốc neo, không ép tính lại tổng quota.', 'A single entry is only an anchor and does not force a new total quota estimate.'],
        ['thành một điểm đo. Nhiều điểm đo sẽ được đối chiếu bằng estimator chống nhiễu/làm tròn để suy ra dung lượng quota. Một lần nhập đơn lẻ chỉ là mốc neo, không ép tính lại tổng quota.', 'as one measurement point. Multiple measurements are reconciled by a noise/rounding-resistant estimator to infer quota capacity. A single entry is only an anchor and does not force a new total quota estimate.'],
        ['Thêm Tài Khoản Gmail Đăng Nhập', 'Add Signed-in Gmail Account'],
        ['Lưu giữ danh bạ tài khoản để dễ dàng theo dõi và chuyển đổi xem hạn mức.', 'Keep a local account list so you can switch quota views easily.'],
        ['Thêm / Cập Nhật Model Codex', 'Add / Update Codex Model'],
        ['Nhập dữ liệu usage từ Codex (OpenAI, Anthropic) để theo dõi tổng hợp.', 'Enter Codex usage data (OpenAI, Anthropic) for combined tracking.'],
        ['Hạn Mức Tuần Codex', 'Codex Weekly Limit'],
        ['Nhập tổng token được cấp mỗi tuần cho gói Codex.', 'Enter the total weekly token allowance for the Codex plan.'],
        ['Tự động đồng bộ:', 'Auto refresh:'],
        ['Tắt (Thủ công)', 'Off (Manual)'],
        ['Mỗi 5 giây', 'Every 5 seconds'],
        ['Mỗi 10 giây', 'Every 10 seconds'],
        ['Mỗi 30 giây', 'Every 30 seconds'],
        ['Mỗi 1 phút', 'Every minute'],
        ['Cỡ ô bảng:', 'Table density:'],
        ['Nhỏ gọn (Hiển thị nhiều dòng nhất)', 'Compact (show the most rows)'],
        ['Vừa phải (Cân đối chuẩn)', 'Comfortable (balanced)'],
        ['Rộng rãi (Khoảng cách thoáng)', 'Spacious (more breathing room)'],
        ['Tùy chỉnh chi tiết kích thước ô bảng', 'Fine-tune table cell sizing'],
        ['Tùy Chỉnh Kích Thước Ô Bảng', 'Custom Table Density'],
        ['📏 Tùy Chỉnh Kích Thước Ô Bảng', '📏 Custom Table Density'],
        ['Độ cao hàng (Dọc):', 'Row height:'],
        ['Khoảng cách cột (Ngang):', 'Column spacing:'],
        ['Cỡ chữ bảng (Font size):', 'Table font size:'],
        ['Cập nhật dữ liệu ngay lập tức', 'Refresh data now'],
        ['Cập nhật ngay', 'Refresh now'],
        ['Xuất báo cáo JSON', 'Export JSON report'],
        ['Xuất dữ liệu', 'Export data'],
        ['Cập nhật lần cuối:', 'Last updated:'],
        ['Nguồn dữ liệu:', 'Data source:'],
        ['Tiêu chuẩn Benchmark:', 'Benchmark standard:'],
        ['Làm mới sau:', 'Next refresh:'],
        ['Giám sát mức tiêu tốn tài nguyên cho Google Antigravity IDE', 'Resource usage monitoring for Google Antigravity IDE'],
        ['Hệ số quy đổi ước lượng: ~3 ký tự/token cho nội dung song ngữ Việt/Anh + mã nguồn', 'Estimated conversion: ~3 characters/token for mixed Vietnamese/English text plus source code'],
        ['Tín Hiệu', 'Signals'],
        ['Tổng Tokens', 'Total Tokens'],
        ['Tất cả Models', 'All Models'],
        ['Tất cả loại công việc', 'All task types'],
        ['Tất cả trạng thái', 'All statuses'],
        ['Tất cả tuyến model', 'All model routes'],
        ['Khoảng đo', 'Range'],
        ['Phạm vi', 'Range'],
        ['Cửa sổ', 'Window'],
        ['30 ngày', '30 days'],
        ['90 ngày', '90 days'],
        ['180 ngày gần nhất', 'Last 180 days'],
        ['365 ngày gần nhất', 'Last 365 days'],
        ['Toàn bộ dữ liệu', 'All data'],
        ['Theo ngày', 'Daily'],
        ['Theo tháng', 'Monthly'],
        ['Theo năm', 'Yearly'],
        ['cân bằng', 'balanced'],
        ['Gần Nhất vs Sol High', 'Latest vs Sol High'],
        ['Độ Tin Cậy', 'Confidence'],
        ['Lớn hơn 1× nghĩa là đốt quota nhanh hơn trên cùng raw token.', '>1× means quota is consumed faster for the same raw-token volume.'],
        ['Lớn hơn 1× nghĩa là task gần đây tiêu hao quota nhiều hơn Sol High trong cùng cửa sổ.', '>1× means recent tasks consumed more quota than Sol High in the same window.'],
        ['Đánh giá theo số khoảng đo, số phiên độc lập và tổng quota quan sát của cả model lẫn Sol High. Lấy mức thấp hơn của hai bên.', 'Confidence uses interval count, independent sessions, and observed quota for both the model and Sol High; the lower side determines the result.'],
        ['Model Gemini (5 Giờ)', 'Gemini Models (5 hours)'],
        ['Claude & External (5 Giờ)', 'Claude & External (5 hours)'],
        ['Hạn Mức Còn Lại', 'Quota Remaining'],
        ['Đã Dùng (5h)', 'Used (5h)'],
        ['Còn Lại', 'Remaining'],
        ['Giới Hạn Tối Đa', 'Maximum Limit'],
        ['Lượt Request (5h)', 'Requests (5h)'],
        ['Hồi phục lần đầu sau:', 'First recovery in:'],
        ['Cập nhật % Quota Chuẩn', 'Update Reference Quota %'],
        ['⚙️ Cập nhật % Quota Chuẩn', '⚙️ Update Reference Quota %'],
        ['Chuyển đổi tài khoản Gmail:', 'Switch Gmail account:'],
        ['Địa chỉ Gmail (*):', 'Gmail address (*):'],
        ['Họ và Tên:', 'Full name:'],
        ['Gói dịch vụ:', 'Plan:'],
        ['Mặc định', 'Default'],
        ['Áp dụng', 'Apply'],
        ['Hủy Bỏ', 'Cancel'],
        ['Đóng', 'Close'],
        ['Tên Model (*):', 'Model name (*):'],
        ['Chế Độ Cập Nhật:', 'Update mode:'],
        ['Tokens All-Time / Thêm:', 'All-Time Tokens / Add:'],
        ['Tokens 7 Ngày Qua:', 'Tokens in Last 7 Days:'],
        ['Chi Phí All-Time / Thêm ($):', 'All-Time Cost / Add ($):'],
        ['Chi Phí 7 Ngày Qua ($):', 'Cost in Last 7 Days ($):'],
        ['Input Tokens:', 'Input Tokens:'],
        ['Output Tokens:', 'Output Tokens:'],
        ['Thinking Tokens:', 'Thinking Tokens:'],
        ['Số Phiên:', 'Sessions:'],
        ['Số Phản Hồi:', 'Responses:'],
        ['Tool Calls:', 'Tool Calls:'],
        ['Hạn mức tuần (tokens):', 'Weekly limit (tokens):'],
        ['Lưu Hạn Mức', 'Save Limit'],
        ['💾 Lưu Hạn Mức', '💾 Save Limit'],
        ['Đồng Bộ Thời Gian Đếm Ngược Reset (Theo IDE Settings):', 'Sync Reset Countdown (from IDE Settings):'],
        ['⏱️ Đồng Bộ Thời Gian Đếm Ngược Reset (Theo IDE Settings):', '⏱️ Sync Reset Countdown (from IDE Settings):'],
        ['Vừa reset chu kỳ (Xóa token dồn trước lúc reset)', 'Cycle just reset (discard pre-reset accumulated tokens)'],
        ['🔄 Vừa reset chu kỳ (Xóa token dồn trước lúc reset)', '🔄 Cycle just reset (discard pre-reset accumulated tokens)'],
        ['Vừa reset chu kỳ tuần', 'Weekly cycle just reset'],
        ['🔄 Vừa reset chu kỳ tuần', '🔄 Weekly cycle just reset'],
        ['Đang lập chỉ mục', 'Indexing'],
        ['Loại công việc', 'Task type'],
        ['Kết quả', 'Outcome'],
        ['Tổng token', 'Total tokens'],
        ['Nguồn Dữ Liệu', 'Data Source'],
        ['Quota Tuần (%)', 'Weekly Quota (%)'],
        ['Bình Thường', 'Normal'],
        ['Token cao', 'High token usage'],
        ['Có lỗi', 'Has errors'],
        ['Thấp', 'Low'],
        ['Vừa', 'Medium'],
        ['Cao', 'High'],
        ['Không rõ model', 'Unknown model'],
        ['Không có mô tả', 'No description'],
        ['Không có bước nào được ghi lại.', 'No recorded steps.'],
        ['Không dùng tool nào', 'No tools used'],
        ['Chưa có prompt', 'No prompt recorded'],
        ['Chưa có dữ liệu', 'No data'],
        ['Chưa đủ mẫu', 'Insufficient samples'],
        ['Chưa đủ dữ liệu', 'Insufficient data'],
        ['Học setup giao dịch', 'Trading setup study'],
        ['Làm web', 'Web development'],
        ['Mô phỏng 3D', '3D simulation'],
        ['Sửa lỗi phần mềm', 'Software debugging'],
        ['Nghiên cứu & kiểm chứng', 'Research & verification'],
        ['Xử lý tài liệu', 'Document processing'],
        ['Chẩn đoán hệ thống', 'System diagnostics'],
        ['Công việc khác', 'Other work'],
        ['Khác', 'Other'],
        ['Chưa xác nhận', 'Unconfirmed'],
        ['Đã bỏ dở', 'Abandoned'],
        ['Đã đạt yêu cầu', 'Accepted'],
        ['Đã đạt', 'Accepted'],
        ['đã hiệu chỉnh', 'reviewed'],
        ['tự động', 'automatic'],
        ['Tự động', 'Automatic'],
        ['Thủ công', 'Manual'],
        ['Nguồn:', 'Source:'],
        ['Bắt đầu:', 'Started:'],
        ['Thời lượng:', 'Duration:'],
        ['Chi phí ước tính:', 'Estimated cost:'],
        ['Tổng Chi Phí Ước Tính (All-Time)', 'Total Estimated Cost (All-Time)'],
        ['Tổng Models Đang Theo Dõi', 'Models Tracked'],
        ['Tokens Theo Nguồn (Không Cộng Chéo)', 'Tokens by Source (No Double Counting)'],
        ['Model Tốn Nhiều Token Nhất', 'Highest Token Usage Model'],
        ['Chưa Có Dữ Liệu Hạn Mức Codex', 'No Codex Quota Data'],
        ['Chưa đọc được hạn mức trực tiếp từ Codex và chưa có bản ghi phiên làm việc để đối chiếu. Hãy kiểm tra Codex đã được cài đặt và đăng nhập.', 'Could not read live Codex quota and no session record is available for fallback. Check that Codex is installed and signed in.'],
        ['Codex trực tiếp', 'Codex live'],
        ['Bản ghi phiên Codex', 'Codex session logs'],
        ['Chưa kết nối trực tiếp; số liệu có thể chưa cập nhật.', 'Live connection unavailable; values may be stale.'],
        ['Ghi nhận:', 'Observed:'],
        ['Gần đây', 'Recently'],
        ['Gói:', 'Plan:'],
        ['Google AI Pro (Mặc định)', 'Google AI Pro (Default)'],
        ['% Đã Dùng', '% Used'],
        ['% Còn Lại', '% Remaining'],
        ['Khung', 'Window'],
        ['Đang dùng', 'In use'],
        ['Gần hết', 'Nearly exhausted'],
        ['Hết hạn mức', 'Quota exhausted'],
        ['Khóa do hết Hạn Mức Tuần', 'Locked by weekly quota'],
        ['Đã đầy 100% (Tối ưu)', 'Full 100% (optimal)'],
        ['Đã đầy 100%', 'Full 100%'],
        ['Đã sẵn sàng 100%', 'Ready 100%'],
        ['Đang hồi phục token...', 'Recovering tokens...'],
        ['Đang reset tuần...', 'Resetting weekly window...'],
        ['Đã hồi phục 100%', 'Recovered to 100%'],
        ['Tín hiệu cần kiểm tra', 'Signals require review'],
        ['Chưa thấy bất thường đáng kể', 'No significant anomaly detected'],
        ['Chi phí cao', 'High cost'],
        ['Có khoảng nghỉ dài', 'Long idle gap'],
        ['Chạy lâu', 'Long runtime'],
        ['Nhiều tools', 'Many tool calls'],
        ['Đổi model', 'Model switch'],
        ['Phiên Cần Kiểm Tra', 'Sessions to Review'],
        ['Phiên Có Lỗi', 'Sessions with Errors'],
        ['Phiên Đổi Model', 'Model-Switch Sessions'],
        ['Mức Lệch Cao Nhất', 'Largest Deviation'],
        ['Điều tra', 'Investigate'],
        ['Xem bước', 'View steps'],
        ['Lịch Sử Tiến Trình', 'Execution History'],
        ['Phân Bổ Công Cụ Đã Dùng', 'Tool Usage Breakdown'],
        ['Tốc Độ Sinh Token', 'Token Generation Speed'],
        ['Giá Trị Theo AA Task', 'Value per AA Task'],
        ['Dùng Nhiều Nhất Cục Bộ', 'Most Used Locally'],
        ['Không có model trong bộ lọc', 'No models in the current filter'],
        ['AA chưa công bố đủ cost/task', 'AA has not published enough cost/task data'],
        ['AA ước tính', 'AA estimate'],
        ['AA đo độc lập', 'AA independently measured'],
        ['AA chưa công bố', 'AA not published'],
        ['đời trước', 'previous generation'],
        ['Dữ liệu lịch sử', 'Historical data'],
        ['Lựa chọn', 'Choice'],
        ['Cục bộ:', 'Local:'],
        ['chưa đủ mẫu quota/task', 'insufficient quota/task samples'],
        ['Mốc Sol High', 'Sol High baseline'],
        ['Chưa đủ mẫu Sol High', 'Insufficient Sol High samples'],
        ['Trung vị', 'Median'],
        ['sửa', 'corrections'],
        ['lượt', 'turns'],
        ['đạt lần đầu', 'first-pass'],
        ['Công dạy', 'extra guidance'],
        ['tin cậy', 'confidence'],
        ['Nhiệm vụ đã ghép', 'Grouped missions'],
        ['Đổi model / subagent', 'Model switch / subagent'],
        ['Đã được anh kiểm tra', 'Manually reviewed'],
        ['Từ các lượt Codex trực tiếp', 'From direct Codex turns'],
        ['Xác nhận hoặc suy ra có độ tin cậy', 'Explicitly confirmed or inferred with confidence'],
        ['Cần kiểm tra trước khi dùng', 'Review before using'],
        ['Giữ thành tuyến, loại khỏi ma trận chính', 'Preserved as a route and excluded from the main matrix'],
        ['Loại việc hoặc kết quả đã sửa tay', 'Task type or outcome manually corrected'],
        ['Ghép mục trước', 'Merge previous'],
        ['Tách lượt cuối', 'Split last turn'],
        ['Trả tự động', 'Reset to automatic'],
        ['Xem', 'View'],
        ['lượt làm việc', 'work turns'],
        ['Có', 'Has'],
        ['lượt subagent', 'subagent turns'],
        ['phí chưa rõ', 'cost unknown'],
        ['dữ liệu cũ', 'stale data'],
        ['token thực từ report', 'exact tokens from report'],
        ['token ước lượng', 'estimated tokens'],
        ['toàn bộ phiên', 'all sessions'],
        ['cùng nguồn + model', 'same source + model'],
        ['cùng nguồn', 'same source'],
        ['Loại khỏi so sánh', 'Excluded from comparison'],
        ['Trên 6h, có thể gồm thời gian nghỉ', 'Over 6h; may include idle time'],
        ['Chuẩn', 'Baseline'],
        ['phiên mẫu', 'sample sessions'],
        ['phiên chat', 'sessions'],
        ['phiên', 'sessions'],
        ['ngày qua', 'days'],
        ['ngày', 'days'],
        ['giờ', 'hours'],
        ['phút', 'minutes'],
        ['lỗi', 'errors'],
        ['lần', 'times'],
        ['còn lại', 'remaining'],
        ['TỔNG CỘNG (THEO NGUỒN)', 'TOTAL (BY SOURCE)'],
        ['AGY Live Usage Tracker • Giám sát mức tiêu tốn tài nguyên cho Google Antigravity IDE', 'AGY Live Usage Tracker • Local resource-usage analysis for AI coding assistants'],
        ['• Giám sát mức tiêu tốn tài nguyên cho Google Antigravity IDE', '• Local resource-usage analysis for AI coding assistants'],
        ['Giám sát mức tiêu tốn tài nguyên cho Google Antigravity IDE', 'Local resource-usage analysis for AI coding assistants'],
        ['Hệ số quy đổi ước lượng: ~3 ký tự/token cho nội dung song ngữ Việt/Anh + mã nguồn', 'Estimated conversion: ~3 characters/token for mixed Vietnamese/English text plus source code'],
        ['ChatGPT Web được hiển thị là GPT-5.6 Sol Thinking. Log cục bộ không cung cấp một mức reasoning effort API ổn định, nên chi phí được ước tính theo bảng giá token của họ GPT-5.6 Sol; giá token giống nhau giữa các mức effort.', 'ChatGPT Web is displayed as GPT-5.6 Sol Thinking. Local logs do not provide a stable API reasoning-effort level, so cost is estimated using the GPT-5.6 Sol family token pricing; token prices are identical across effort levels.'],
    ].sort((a, b) => b[0].length - a[0].length);

    const DYNAMIC_RULES = [
        [/^~(\d+) phút$/, '~$1 minutes'],
        [/^~(\d+) giờ$/, '~$1 hours'],
        [/^(\d+) phút$/, '$1 minutes'],
        [/^(\d+) giờ$/, '$1 hours'],
        [/^(\d+) ngày$/, '$1 days'],
        [/^Sau ~(\d+) phút$/, 'In ~$1 minutes'],
        [/^Sau ~(\d+) giờ$/, 'In ~$1 hours'],
        [/^Sau ~(\d+) ngày (\d+)h$/, 'In ~$1 days $2h'],
        [/^(\d+) phiên$/, '$1 sessions'],
        [/^(\d+) lần$/, '$1 times'],
        [/^(\d+) biến thể$/, '$1 variants'],
        [/^(\d+) nhiệm vụ$/, '$1 missions'],
    ];

    function preferredLanguage() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (SUPPORTED.has(saved)) return saved;
        } catch (_) {
            // localStorage can be unavailable in privacy-restricted contexts.
        }
        const browserLanguage = String(navigator.language || navigator.userLanguage || '').toLowerCase();
        return browserLanguage.startsWith('en') ? 'en' : 'vi';
    }

    const EN_EXACT = new Map(EN);

    function translateString(value, target = language) {
        if (target !== 'en' || value === null || value === undefined) return String(value ?? '');

        const source = String(value);
        const trimmed = source.trim();
        if (!trimmed) return source;
        const normalized = trimmed.replace(/\s+/g, ' ');

        // Keep static UI translation deterministic. Substring replacement looked
        // convenient at first, but short Vietnamese words such as "ngày", "lần",
        // "phiên" and "tự động" produced half-Vietnamese/half-English sentences.
        // Translate complete text nodes/attributes first, then use explicit regexes
        // only for values that genuinely contain runtime data.
        const exact = EN_EXACT.get(trimmed) ?? EN_EXACT.get(normalized);
        if (exact !== undefined) {
            const leading = source.slice(0, source.indexOf(trimmed));
            const trailing = source.slice(source.indexOf(trimmed) + trimmed.length);
            return `${leading}${exact}${trailing}`;
        }

        for (const [pattern, replacement] of DYNAMIC_RULES) {
            if (pattern.test(normalized)) {
                const translated = normalized.replace(pattern, replacement);
                const leading = source.slice(0, source.indexOf(trimmed));
                const trailing = source.slice(source.indexOf(trimmed) + trimmed.length);
                return `${leading}${translated}${trailing}`;
            }
        }

        return source;
    }

    function shouldSkip(node) {
        const parent = node.parentElement;
        if (!parent) return false;
        return Boolean(parent.closest('script, style, noscript, code, pre, svg'));
    }

    function translateTextNode(node) {
        if (!node || node.nodeType !== Node.TEXT_NODE || shouldSkip(node)) return;
        const current = node.nodeValue || '';
        if (!current.trim()) return;
        const previousOriginal = originalText.get(node);
        const previousTranslated = previousOriginal === undefined ? null : translateString(previousOriginal, 'en');
        if (language === 'en') {
            if (previousOriginal === undefined || current !== previousTranslated) originalText.set(node, current);
            const canonical = originalText.get(node) ?? current;
            const translated = translateString(canonical, 'en');
            if (node.nodeValue !== translated) node.nodeValue = translated;
        } else if (previousOriginal !== undefined && node.nodeValue !== previousOriginal) {
            node.nodeValue = previousOriginal;
        }
    }

    function translateElementAttributes(el) {
        if (!(el instanceof Element)) return;
        const attrs = ['title', 'placeholder', 'aria-label'];
        let stored = originalAttrs.get(el);
        if (!stored) {
            stored = {};
            originalAttrs.set(el, stored);
        }
        attrs.forEach(name => {
            if (!el.hasAttribute(name)) return;
            const current = el.getAttribute(name) || '';
            const oldOriginal = stored[name];
            const oldTranslated = oldOriginal === undefined ? null : translateString(oldOriginal, 'en');
            if (language === 'en') {
                if (oldOriginal === undefined || current !== oldTranslated) stored[name] = current;
                const translated = translateString(stored[name], 'en');
                if (current !== translated) el.setAttribute(name, translated);
            } else if (oldOriginal !== undefined && current !== oldOriginal) {
                el.setAttribute(name, oldOriginal);
            }
        });
    }

    function translateTree(root = document.body) {
        if (!root) return;
        applying = true;
        try {
            if (root.nodeType === Node.TEXT_NODE) translateTextNode(root);
            if (root instanceof Element) translateElementAttributes(root);
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
            let node = walker.nextNode();
            while (node) {
                if (node.nodeType === Node.TEXT_NODE) translateTextNode(node);
                else translateElementAttributes(node);
                node = walker.nextNode();
            }
        } finally {
            applying = false;
        }
    }

    function updateDocumentMetadata() {
        document.documentElement.lang = language;
        document.title = language === 'en'
            ? 'AGY Live Usage Tracker — Detailed Usage & Cost Analytics'
            : 'AGY Live Usage Tracker — Theo Dõi & Phân Tích Chi Tiết';
        const description = document.querySelector('meta[name="description"]');
        if (description) {
            description.content = language === 'en'
                ? 'Local-first dashboard for investigating AI coding-assistant usage, tokens, quota, cost, tool calls, models, and task outcomes.'
                : 'Dashboard phân tích lượng usage, tokens, chi phí và tool calls trong Antigravity IDE theo thời gian thực';
        }
        const select = document.getElementById('language-select');
        if (select && select.value !== language) select.value = language;
    }

    function setLanguage(next, { persist = true, announce = true } = {}) {
        if (!SUPPORTED.has(next)) return;
        language = next;
        if (persist) {
            try { localStorage.setItem(STORAGE_KEY, language); } catch (_) {}
        }
        updateDocumentMetadata();
        translateTree(document.body);
        if (announce) {
            window.dispatchEvent(new CustomEvent('usage-tracker:language-changed', { detail: { language } }));
        }
    }

    function untranslatedVietnamese(root = document.body) {
        const hits = [];
        if (!root) return hits;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            if (!shouldSkip(node)) {
                const text = String(node.nodeValue || '').replace(/\s+/g, ' ').trim();
                if (text && /[À-ỹĐđ]/.test(text)) hits.push(text);
            }
            node = walker.nextNode();
        }
        const elements = root instanceof Element ? [root, ...root.querySelectorAll('*')] : [...document.querySelectorAll('*')];
        elements.forEach(el => {
            ['title', 'placeholder', 'aria-label'].forEach(name => {
                if (!el.hasAttribute(name)) return;
                const value = String(el.getAttribute(name) || '').replace(/\s+/g, ' ').trim();
                if (value && /[À-ỹĐđ]/.test(value)) hits.push(`${name}: ${value}`);
            });
        });
        return [...new Set(hits)];
    }

    const observer = new MutationObserver(mutations => {
        if (applying) return;
        applying = true;
        try {
            mutations.forEach(mutation => {
                if (mutation.type === 'characterData') {
                    translateTextNode(mutation.target);
                } else if (mutation.type === 'childList') {
                    mutation.addedNodes.forEach(node => {
                        if (node.nodeType === Node.TEXT_NODE) translateTextNode(node);
                        else if (node.nodeType === Node.ELEMENT_NODE) translateTree(node);
                    });
                } else if (mutation.type === 'attributes') {
                    translateElementAttributes(mutation.target);
                }
            });
        } finally {
            applying = false;
        }
    });

    const nativeFillText = CanvasRenderingContext2D?.prototype?.fillText;
    if (nativeFillText) {
        CanvasRenderingContext2D.prototype.fillText = function patchedFillText(text, ...args) {
            return nativeFillText.call(this, translateString(text), ...args);
        };
    }
    const nativeStrokeText = CanvasRenderingContext2D?.prototype?.strokeText;
    if (nativeStrokeText) {
        CanvasRenderingContext2D.prototype.strokeText = function patchedStrokeText(text, ...args) {
            return nativeStrokeText.call(this, translateString(text), ...args);
        };
    }

    window.UsageI18n = {
        get language() { return language; },
        setLanguage,
        t: translateString,
        translateTree,
        auditUntranslated: untranslatedVietnamese,
    };

    document.addEventListener('DOMContentLoaded', () => {
        language = preferredLanguage();
        updateDocumentMetadata();
        const select = document.getElementById('language-select');
        if (select) {
            select.value = language;
            select.addEventListener('change', event => setLanguage(event.target.value));
        }
        translateTree(document.body);
        observer.observe(document.body, {
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
            attributeFilter: ['title', 'placeholder', 'aria-label'],
        });
    });
})();
