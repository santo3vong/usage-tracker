# Usage Tracker

> Theo dõi usage, quota, cost và hiệu quả theo task cho Codex, Gemini/Antigravity và các workflow trợ lý lập trình trên Windows — chạy local trên máy của bạn.

**Tiếng Việt** · [English](README.en.md)

Ứng dụng này giúp bạn theo dõi các thông số thiết thực để quản lý hạn mức AI.

> Các ảnh dưới đây là dữ liệu thực chụp ngày 25/09/2026; số liệu thay đổi theo tài khoản, model và thời điểm đo. Các mức USD là chi phí tương đương ước tính, không phải hóa đơn Codex.

## Tự cập nhật Bảng Xếp Hạng Models

Bảng xếp hạng kiểm tra [Artificial Analysis Data API](https://artificialanalysis.ai/data-api/docs) khi tải dữ liệu và bản chụp cũ hơn 24 giờ, rồi lưu bản hợp lệ gần nhất để vẫn xem được lúc mất mạng. Tạo key ở Artificial Analysis và đặt vào biến môi trường `ARTIFICIAL_ANALYSIS_API_KEY` của tiến trình server, hoặc lưu một dòng key trong `C:\Users\<tên người dùng>\.codex\usage-tracker-aa-api-key.txt` (nằm ngoài thư mục web). Cache được Git bỏ qua; không đưa key vào giao diện hay commit. Sau khi thêm key, khởi động lại server và tải lại trang. Nếu chưa có key, bảng dùng bản chụp AA được xác minh gần nhất và ghi rõ ngày. Model có trong bộ chọn Codex nhưng AA chưa chấm vẫn hiện là chưa có hạng; giá API/Codex được quản lý riêng, không suy ra từ điểm AA.

##  Usage Tracker này có gì đặc biệt hơn

Nếu các bạn chịu khó lên x.com để hóng tin AI thì hẳn bạn cũng đã đụng một số người có ý tưởng dùng sol làm orchestrator và luna làm executor để tiết kiệm token, tuy nhiên hiệu quả tiết kiệm còn khá trồi sụt và chưa thật sự hiệu quả ở phần lớn user, vì có lẽ là do đặc thù công việc ở mỗi người làm, ở những người mà có công việc sáng tạo hoặc phải brainstorm nhiều thì workload ấy có khi còn tốn token hơn, vậy nên do tùy đặc thù công việc mỗi người, thì tốn hay ko thì sẽ khác nhau, vì vậy repo này sinh ra như một công cụ giúp bạn ước lượng được model này sẽ tiêu tốn bao nhiêu hạn mức cho công việc đấy, từ đó sẽ giúp các bạn chọn model một cách hợp lí hơn.
Một trường hợp khác là trước đây Tibo là trưởng bộ phận codex cũng nói rằng là astra sẽ đỡ tốn hơn so với sol , nhưng cuối cùng có đo thì mới biết được là  nó còn phải tùy thuộc theo đặc thù công việc đã, ví dụ như của tôi, 5.6 sol extra high là tốn hơn 1.5 lần so với sol high, vẫn tiết kiệm hơn với các mô hình astra là phải gấp 3 lần hạn mức so với sol high, từ đó tôi thấy được là với công việc của tôi, nếu độ thông mình của sol extra high là đã đủ cho xử lí công việc, và khối lượng công việc còn nhiều, thì cứ theo 5.6 sol extra high mà giã. đồng thời cũng kiểm chứng được thông tin mà giới lập trình khi test astra trong giai đoạn đầu báo rằng là astra extra high/ max đỡ tốn hơn so với astra low/medium, thực tế công việc của tôi thì đúng là astra extra high là tiết kiệm nhất trong các mô hình, low và medium lại tốn hơn nhiều

Từ những nhu cầu đo đạc đó, Usage Tracker tập trung giải quyết các câu hỏi cụ thể:

Codex còn bao nhiêu quota trong khung 5 giờ và khung tuần?

Con số hiển thị lấy trực tiếp từ log hay đang là mức ước tính (estimate)?

Mô hình đang tiêu hao hạn mức ở tốc độ nào tại thời điểm hiện tại?

Với những task tương đương, mức sử dụng thông thường rơi vào khoảng bao nhiêu?

Task đã hoàn tất hay cần thêm các lượt re-teach và chỉnh sửa?

Khi phân luồng qua orchestrator, executor hay subagent, mức tiêu hao được ghi nhận cho nhánh nào?

Các chỉ số chưa có dữ liệu sẽ hiển thị unknown, tránh việc gán mặc định bằng 0 gây sai lệch thống kê.

## Các biểu đồ thực tế

**Trần hạn mức Codex theo tuần.** Mỗi chấm là một lần suy luận ngân sách tuần từ chi phí tương đương và phần trăm hạn mức đã dùng. Đường gần đây phản ứng với thay đổi mới, còn đường hội tụ tổng hợp các phép đo được giữ lại. Chọn tài khoản, phạm vi và cửa sổ để tránh trộn các tài khoản khác nhau.

![Biểu đồ suy luận trần hạn mức Codex theo tuần](docs/screenshots/codex-weekly-capacity-2026-09-25.png)

**Hiệu suất hạn mức 5 giờ theo thời gian.** Đường của từng model so với Sol High trên cùng raw token; bảng bên dưới ghi số mẫu, độ tin cậy và liệu mốc so sánh là trực tiếp hay bắc cầu. Giá trị trên 1× nghĩa là tiêu hao hạn mức nhanh hơn, không phải thước đo chất lượng model.

![Xu hướng hiệu suất hạn mức Codex 5 giờ](docs/screenshots/quota-efficiency-trend-2026-09-25.png)

**Hao phí hạn mức cho mỗi task.** Biểu đồ này lấy các task hoàn tất gần đây, so với Sol High trong cùng cửa sổ và chỉ hiện model có đủ mẫu. Nó đo chi phí hoàn thành một task, khác với tốc độ tiêu hao trên một token ở biểu đồ trên.

![Xu hướng hao phí hạn mức theo task](docs/screenshots/task-quota-trend-2026-09-25.png)

**Token và chi phí theo model.** Hai biểu đồ cột chồng cùng dùng phạm vi và đơn vị ngày/tháng/năm; hover lên cột để xem toàn bộ model trong mốc đó. USD là giá trị ước tính, còn Fast dùng hệ số credit ChatGPT khi quy đổi.

![Biểu đồ token và chi phí theo model](docs/screenshots/token-cost-trend-2026-09-25.png)

Biểu đồ đường **Thời Gian Trung Bình Hoàn Thành Nhiệm Vụ** bổ sung thời gian xử lý thực đo của các lượt Codex đã hoàn tất, với phạm vi 30/90/180 ngày và cửa sổ trung bình 7/14/30 ngày. Khoảng chờ giữa các lần sửa được loại khỏi thời gian xử lý; các nhiệm vụ thiếu mốc hoàn tất không được gán thời lượng bằng 0.

## Tính năng: usage tức thời/ usage trung bình

Tracker không chỉ hiển thị một cột tổng token đã dùng, vì một con số đơn lẻ khó phản ánh đúng tình trạng vận hành:

Usage tức thời (current / instantaneous): Cho biết tốc độ sử dụng hạn mức của mô hình hoặc tác vụ ngay lúc đang chạy. Chỉ số này giúp theo dõi và cân nhắc xem có nên duy trì luồng xử lý hiện tại hay không. Tuy nhiên, giá trị tức thời thường dễ biến động khi gặp các task có context lớn đột biến hoặc yêu cầu suy luận dài.

Usage trung bình (average / typical): Đóng vai trò làm mốc tham chiếu cho các loại công việc tương đương. Để hạn chế ảnh hưởng từ các trường hợp bất thường (outliers), tracker ưu tiên sử dụng trung vị (median) khi đã thu thập đủ số lượng mẫu.

Việc kết hợp cả hai chỉ số giúp bạn vừa có cảnh báo sớm khi một task phát sinh chi phí bất thường, vừa có mốc nền tảng (baseline) để so sánh và lựa chọn mô hình phù hợp.

## Ma trận về độ tương quan hạn mức tiêu tốn / hoàn thành 1 task

a. Vấn đề của cách đo cũ: Đánh giá theo turn đơn lẻ là chưa phản ánh đúng thực tế
Hiện tại, việc nhìn vào tốc độ tiêu hao tức thời hoặc lượng token của từng lượt phản hồi riêng lẻ (single turn) rất dễ gây hiểu lầm khi chọn model:

Với task đơn giản: Cả model mạnh lẫn model yếu đều có thể làm xong ngay trong một lần chạy. Khi đó, việc bật effort cao (như Sol Extra High) rõ ràng gây lãng phí thừa (thực tế có thể ngốn gấp 1.84 lần so với Sol High).

Với task phức tạp (như học setup giao dịch, lập trình web, mô phỏng 3D...):

Các model nhẹ hoặc effort thấp (như GPT-5.5 Low, Sol High) nhìn qua từng turn thì rất rẻ. Nhưng nếu model chưa nắm bắt được vấn đề, người dùng phải giải thích lại nhiều lần, ra lệnh chỉnh sửa liên tục do làm chưa đạt yêu cầu. Mỗi lần sửa là context lại dồn lên, khiến tổng lượng token cộng dồn cho cả nhiệm vụ bị đội lên rất nhiều.

Ngược lại, model mạnh hơn (như 5.6 Sol Extra High) có chi phí mỗi lượt chạy cao hơn, nhưng khả năng hiểu sâu giúp xử lý dứt điểm chỉ sau 1–2 lần tương tác, không mất công "dạy đi dạy lại". Tính trên toàn bộ nhiệm vụ từ đầu đến cuối, tổng token tiêu hao thực tế đôi khi lại tiết kiệm hơn.

Vì vậy, tracker chuyển từ việc đo từng turn riêng lẻ sang đo toàn bộ mức tiêu hao để hoàn thành một nhiệm vụ. Raw token vẫn được giữ để kiểm tra, còn hệ số chính của ma trận ưu tiên phần trăm hạn mức Codex 5 giờ đã đo; điểm phải bắc cầu hoặc ước lượng luôn kèm nguồn và độ tin cậy.

b. Phương pháp thu thập và tính toán dữ liệu
Dựa trên log lịch sử các task đã chạy:

Với model xử lý tốt: Giao lệnh một lần hoàn thành, không phát sinh thêm lượt chỉnh sửa -> Lấy trực tiếp token của session/turn đó.

Với model cần can thiệp: Bao gồm toàn bộ các lượt re-teaching, nhắc lại yêu cầu, sửa lỗi code hoặc điều chỉnh logic cho đến khi kết quả được nghiệm thu.

Khi đổi model để sửa kết quả chưa đạt, lượt sửa được giữ thành một mission riêng để model sửa vẫn chịu đúng mức sử dụng của nó. Đồng thời, mức tiêu hao của lượt sửa được cộng như khoản phạt cho model tạo ra kết quả chưa đạt. Với chuỗi sửa nhiều model, khoản sửa tiếp theo được truyền ngược cho các mắt xích thất bại trước đó thay vì gộp toàn bộ chuỗi thành một mission trộn.

c. Cấu trúc bảng ma trận 2 chiều
Bảng được thiết kế để lượng hóa năng lực và mức độ tiêu hao của từng cặp Model + Effort trên từng nhóm việc cụ thể:

Cột dọc (Loại nhiệm vụ): Phân theo các công việc thực tế hay làm:

Học / Huấn luyện setup giao dịch

Lập trình Web

Mô phỏng 3D / Xử lý thuật toán

(Các đầu việc đặc thù khác...)

Cột ngang (Model + Effort): Các cấu hình đem ra đối chiếu (GPT-5.5 Low/High/XHigh, Sol High/XHigh, Astra...).

Mốc chuẩn tham chiếu (Baseline): Lấy Sol High làm mốc chuẩn 1.00x.

Các ô còn lại hiển thị hệ số tương quan hạn mức 5 giờ để hoàn thành nhiệm vụ, lấy Sol High trong cùng loại việc làm mốc. Nếu chưa đủ điểm hạn mức trực tiếp, tracker chỉ dùng đường bắc cầu có bằng chứng và ghi rõ đây là ước lượng; hệ số raw token vẫn xuất hiện như số liệu hỗ trợ.

Hệ số < 1.00x: Tiêu hao hạn mức 5 giờ ít hơn Sol High trên cùng loại việc.

Hệ số > 1.00x: Tiêu hao hạn mức 5 giờ nhiều hơn Sol High.

Xử lý thiếu số liệu: Với những bài toán chưa từng chạy trên một cấu hình model nhất định, ô tương ứng hiển thị rõ Chưa có dữ liệu, không tự ý nội suy hay gán mặc định bằng 0.

d. Giá trị mang lại
Chọn model theo số liệu định lượng: Nhìn vào ma trận sẽ biết rõ: loại việc nào đơn giản để giao cho model nhẹ nhằm tiết kiệm quota, và loại việc nào bắt buộc phải dùng model mạnh ngay từ đầu để tránh vòng lặp sửa lỗi tốn kém.

Các thiết lập gần nhất như ngôn ngữ, model, bộ lọc, khoảng thời gian, đơn vị biểu đồ, kích thước bảng và độ rộng cột được lưu trong trình duyệt để lần mở sau giữ nguyên cách xem.

<img width="1757" height="687" alt="Ma trận hạn mức theo loại nhiệm vụ" src="https://github.com/user-attachments/assets/224ce827-b99f-4fa6-95a6-d43466856c43" />


## Tự động phân nhóm chỉ là gợi ý

Mission grouping dùng heuristic nên sẽ có trường hợp đoán sai ranh giới hoặc sai loại công việc. Vì thế dashboard có phần review thủ công để:

- ghép mission với mission trước;
- tách lượt cuối thành mission mới;
- sửa category;
- xác nhận `accepted`, `unresolved` hoặc `abandoned`;
- trả ranh giới về heuristic tự động.

Automation ở đây giúp giảm công kiểm tra, nhưng không được phép biến một suy đoán thành ground truth chỉ vì máy tự sinh ra nó.

Rule tự động hiện ưu tiên những trường hợp có bằng chứng rõ: một yêu cầu sửa ngay sau nhiệm vụ trong cùng task, đổi model để xử lý chính lỗi đó, câu hỏi riêng về quota/chi phí, hoặc một yêu cầu mới sau khoảng nghỉ dài. Câu “làm tiếp” rõ ràng vẫn nối vào mission cũ. Một câu có từ follow-up nhưng chứa yêu cầu mới sau khoảng nghỉ dài sẽ được tách. Hệ thống không tự nối một lượt sửa ở task khác nếu người dùng không dẫn chính xác task cũ; các trường hợp đó vẫn để review thủ công.

## Từ quota đoán sang quota live

Phiên bản đầu tiên dựa nhiều vào session/transcript cục bộ. Nó hữu ích để tái dựng lịch sử, nhưng không đủ để trả lời chắc chắn Codex còn bao nhiêu quota ngay lúc này.

Tracker được mở rộng theo từng lớp:

1. Quét session cục bộ và chuẩn hóa record để hạn chế đếm trùng.
2. Tách khung 5 giờ và 7 ngày thay vì gộp thành một con số quota chung.
3. Khi local Codex hỗ trợ, đọc rate-limit trực tiếp từ Codex app-server.
4. Gắn provenance để UI phân biệt live source với fallback từ session log.

Một lỗi thực tế từng gặp là tiến trình desktop không tìm thấy executable `codex` theo `PATH`. Tracker vẫn chạy, nhưng âm thầm rơi về session-log fallback, khiến số trông hợp lý nhưng đã cũ.

Từ đó dự án giữ nguyên tắc: **live app-server là nguồn có thẩm quyền khi kết nối được; fallback vẫn hữu ích nhưng phải được ghi nhãn rõ ràng**.

Ở mục hạn mức Codex, biểu đồ **Trần Hạn Mức Codex Tuần Tương Đương Theo USD** lấy từng khoảng tăng của phần trăm quota tuần trong cùng phiên và cùng model/effort, tính chi phí API tương đương của token phát sinh rồi quy ra 100% tuần. Mỗi khoảng là một điểm đo; trung vị hội tụ dùng toàn bộ lịch sử còn lưu, còn trung vị gần đây dùng cửa sổ 7, 14 hoặc 30 ngày để phản ánh thay đổi mới. Trục ngang là thứ tự lần đo, có mốc ngày để đối chiếu. Đây là phép suy luận theo bảng giá API hiện có, không phải hạn mức USD hay credit chính thức; phiên đồng thời, cơ cấu model và thay đổi giá có thể ảnh hưởng kết quả.

Từ các lần đo mới, tracker lưu ID tài khoản Codex dạng UUID cùng bản chụp hạn mức trực tiếp trong tệp cục bộ `~/.codex/usage-tracker-quota-identity.json` (ngoài thư mục web). Chỉ gắn ID cho log token gần thời điểm bản chụp và khớp cả mốc reset 5 giờ lẫn tuần; lịch sử cũ không đủ bằng chứng vẫn thuộc nhóm “Lịch sử chưa rõ tài khoản”. Bộ chọn tài khoản tách trung vị của từng nhóm. Dấu vết đổi ID được ghi là đổi tài khoản; chu kỳ tuần đổi sớm được ghi là reset chưa rõ nguyên nhân. Không tự gọi đó là global reset của OpenAI nếu không có bằng chứng công bố và xác nhận áp dụng cho tài khoản.


## “Token” trên máy không phải lúc nào cũng đến từ cùng một nguồn. Tracker có thể gặp:

- transcript-estimated token;
- exact token từ worker/report;
- token đọc tự động từ Codex session log;
- số manual/configured dùng làm fallback.

Nếu dồn tất cả vào một cột mà không giữ provenance, một estimate và một exact measurement sẽ trông giống hệt nhau. Vì thế tracker cố giữ nguồn đi cùng số liệu.

Tương tự, nếu cost hoặc usage chưa biết thì phải giữ là `unknown`. `Unknown` không đồng nghĩa với `$0` hay `0 token`.

## ChatGPT Web và cached input

Usage Tracker cũng cố tránh kết luận quá mức từ telemetry thiếu dữ liệu. Nếu một nguồn ChatGPT Web cho `cached_input_tokens = 0`, điều đó chỉ có nghĩa tracker không nhận được cache telemetry hữu ích từ nguồn đó; nó **không chứng minh** nền tảng không dùng cache.

Vì vậy cost của ChatGPT Web được xem là estimate/proxy khi không có authoritative source tương ứng, và không nên giả vờ rằng nó cache-comparable với một nguồn có cache telemetry đầy đủ.


## Cài đặt và chạy nhanh

Yêu cầu hiện tại:

- Windows 10 hoặc Windows 11.
- Python 3. Tracker ưu tiên Python runtime đi kèm Codex nếu tìm thấy, sau đó fallback sang `py -3` hoặc `python`.
- Trình duyệt hiện đại.
- Node.js chỉ cần cho kiểm tra JavaScript khi development/CI.

Từ thư mục repo:

```powershell
.\start.bat
```

Hoặc chạy server mà không tự mở browser:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-background.ps1
```

Sau đó mở:

```text
http://127.0.0.1:5051/
```

Dừng hoặc restart server:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop-server.ps1
powershell -ExecutionPolicy Bypass -File .\restart-server.ps1
```

PID và log runtime nằm dưới `runtime/`.

## Độ chính xác và cách đọc số liệu

Không phải mọi con số trên dashboard đều có cùng độ chắc chắn. Hãy đọc source/provenance đi cùng số liệu:

- **Live / exact**: đọc trực tiếp từ nguồn local hỗ trợ giá trị đó.
- **Log-derived**: tính từ session/transcript trên máy.
- **Estimated / inferred**: suy ra từ observation hoặc proxy.
- **Manual / configured**: do người dùng nhập hoặc cấu hình làm fallback.

Cost estimate và quota-capacity estimate là công cụ phân tích, không phải billing record chính thức. Task-outcome classifier và mission grouping là heuristic; kết quả quan trọng hoặc ít mẫu nên được review trước khi dùng làm benchmark.

## Dữ liệu cục bộ và quyền riêng tư

Các file sau được `.gitignore` cố tình loại khỏi Git:

- `accounts.json`
- `codex_usage.json`
- `codex_models_cache.json`
- `codex_mission_turns_cache.json`
- `codex_mission_reviews.json`
- `quota_observations.json`
- `real_quotas.json`
- `time_series_history.json`
- `data.js`
- `runtime/`
- `runtime_backups/`

Chúng có thể chứa account identifier, prompt, local path, usage history hoặc dữ liệu riêng trên máy. **Không dùng `git add -f` để đưa các file này vào public commit nếu chưa tự kiểm tra nội dung.**

Repo public không cần và không nên chứa project riêng, prompt lịch sử cá nhân hay dữ liệu trading riêng của người dùng.

Bốn ảnh mang ngày `2026-09-25` trong `docs/screenshots/` là ảnh chụp thực tế do chủ repo cung cấp; các ảnh demo cũ dùng số liệu minh họa. Tránh đưa prompt riêng hoặc mã tài khoản đầy đủ vào ảnh public mới.

## Development và kiểm thử

```powershell
node --check app.js
node --check i18n.js
python -B -m unittest discover -v
python -B -m py_compile server.py run_server.py
git diff --check
```

Test hiện bao phủ các phần chính như usage source, quota estimator, model catalog, Codex task outcomes và Codex app-server rate-limit handling. GitHub Actions chạy các core check trên Windows.

## Đóng góp, bảo mật và giấy phép

Xem `CONTRIBUTING.md` để biết quy trình development/pull request và `SECURITY.md` để báo lỗi bảo mật hoặc quyền riêng tư.

Giấy phép: MIT, xem `LICENSE`.
