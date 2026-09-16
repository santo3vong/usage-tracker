# Usage Tracker

> Local-first usage, quota, cost, and task-outcome analysis for Codex, Gemini/Antigravity, and other coding-assistant workflows on Windows.

[Tiếng Việt](#tiếng-việt) · [English](#english)

Usage Tracker runs on your own machine. It reads supported local logs/transcripts, uses live local quota sources when they are available, and turns those signals into one browser dashboard without uploading your usage history to a hosted Usage Tracker service.

---

# Tiếng Việt

## Vì sao tôi làm Usage Tracker

Ban đầu vấn đề của tôi rất đơn giản: tôi dùng nhiều trợ lý lập trình, nhưng thứ tôi thực sự cần biết lại không được các dashboard thông thường trả lời đủ rõ.

Tôi không chỉ muốn biết “đã dùng bao nhiêu token”. Tôi muốn biết:

- Codex còn bao nhiêu hạn mức trong khung 5 giờ và khung tuần?
- Con số đó là dữ liệu trực tiếp, dữ liệu từ log hay chỉ là ước tính?
- Nếu một model tốn rất nhiều token, nó có thực sự giải quyết xong công việc hay tôi còn phải sửa, giảng lại và chạy tiếp nhiều lượt?
- Khi đổi model hoặc gọi subagent, phần công việc đó phải được tính cho ai?
- Một con số không biết có thể để là “chưa biết” hay hệ thống lại vô tình biến nó thành `0`?

Usage Tracker lớn dần từ chính các câu hỏi đó. Vì vậy repo này không chỉ là một bảng cộng token; phần lớn chức năng đều xuất hiện sau một lỗi đo lường hoặc một câu hỏi thực tế mà số liệu thô chưa trả lời được.

## Từ bài toán hạn mức đến nguồn dữ liệu trực tiếp

Phiên bản đầu tiên dựa nhiều vào session/transcript cục bộ. Cách đó hữu ích để biết đã làm gì, nhưng nó không đủ để trả lời chắc chắn hạn mức Codex đang còn bao nhiêu ngay lúc này.

Vì thế tracker được mở rộng theo từng lớp:

1. Quét session cục bộ và chuẩn hóa các bản ghi để hạn chế đếm trùng.
2. Tách riêng khung 5 giờ và khung 7 ngày thay vì gộp thành một con số “quota”.
3. Khi Codex cục bộ hỗ trợ, đọc rate-limit trực tiếp từ Codex app-server.
4. Gắn nguồn cho dữ liệu để UI có thể phân biệt dữ liệu live với fallback từ session log.

Điểm này quan trọng vì trong sử dụng thực tế đã từng có một lỗi rất khó nhận ra: khi tracker được mở từ desktop, tiến trình có lúc không tìm thấy executable `codex` theo `PATH`. Hệ thống vẫn chạy, nhưng âm thầm rơi về session-log fallback, nên con số nhìn có vẻ hợp lý nhưng đã cũ.

Từ lỗi đó, nguyên tắc của dự án trở thành: **live app-server là nguồn có thẩm quyền khi kết nối được; fallback vẫn được dùng nhưng phải được ghi nhãn rõ ràng**. Một lỗi khác từng xảy ra là browser giữ bản `app.js` cũ trong cache; vì vậy các static asset chính được version-bust để việc sửa tracker thực sự xuất hiện trên trình duyệt sau khi reload.

## Vì sao hiệu chuẩn quota không thể chỉ lấy một lần nhập %

Một bước tiếp theo là bài toán quy đổi giữa token quan sát được và phần trăm quota còn lại.

Ban đầu cách dễ nhất là nhập một giá trị phần trăm rồi suy ngược ra toàn bộ dung lượng. Nhưng trong thực tế, phần trăm trên IDE thường bị làm tròn; hai lần đọc có thể thuộc hai chu kỳ reset khác nhau; nguồn token có thể khác nhau; và một điểm đo đơn lẻ rất dễ làm hệ thống “học” sai.

Vì thế bộ hiệu chuẩn hiện tại coi mỗi lần nhập là **một điểm quan sát**, không phải chân lý tuyệt đối. Estimator dùng nhiều quan sát, ưu tiên các cặp cùng chu kỳ, xét khoảng làm tròn của phần trăm, tách reset, cho phép bằng chứng cũ hết hiệu lực dần, giữ capacity riêng theo nguồn, và giữ lại prior khi bằng chứng mới chưa đủ mạnh.

Nói ngắn gọn: một lần nhập % chỉ giúp neo mô hình; nhiều điểm đo nhất quán mới đủ để thay đổi kết luận.

## Không trộn các loại bằng chứng

Trong quá trình phát triển tôi gặp thêm một vấn đề: “token” không phải lúc nào cũng đến từ cùng một loại nguồn.

Tracker có thể gặp:

- token ước tính từ transcript;
- token exact từ worker/report;
- token đọc tự động từ Codex session log;
- số liệu nhập tay hoặc cấu hình fallback.

Nếu gộp tất cả thành một cột mà không giữ provenance, một con số chính xác và một con số ước tính sẽ trông giống hệt nhau. Vì thế Usage Tracker cố giữ các nguồn này tách biệt trong dữ liệu và trong phần mô tả UI. Tương tự, khi chi phí hoặc usage chưa biết, tracker không nên biến “unknown” thành `$0` hay `0 token` chỉ để bảng trông đầy đủ hơn.

## Token thô vẫn chưa trả lời được model nào thực sự hiệu quả

Đây là bước làm dự án thay đổi nhiều nhất.

Nếu một task bắt đầu bằng một prompt, sau đó tôi phải sửa yêu cầu, giải thích lại, yêu cầu tiếp tục, hoặc nhờ model sửa phần nó vừa làm, thì việc so từng turn riêng lẻ rất dễ đánh giá sai. Một model có thể có một turn đầu rẻ nhưng tổng chi phí để đi đến kết quả được chấp nhận lại cao hơn nhiều.

Vì thế tracker bổ sung khái niệm **mission**: tính từ khi một mục tiêu bắt đầu cho tới khi mục tiêu đó thực sự đạt yêu cầu hoặc vẫn chưa được xác nhận.

Mission scanner:

- đọc cả Codex `sessions` đang hoạt động và `archived_sessions`;
- gom các lượt sửa, follow-up và tiếp tục liên quan vào cùng một mục tiêu;
- giữ model, effort và route thay vì chỉ giữ một tổng token vô danh;
- cố gắn phần việc của subagent về nhiệm vụ cha;
- đánh dấu route có đổi model hoặc có delegated work là `pure_model=false` để không quy toàn bộ công cho một model duy nhất.

Từ đó dashboard mới có thể đặt câu hỏi hữu ích hơn: **từ lúc bắt đầu một việc đến khi người dùng chấp nhận kết quả, model đã tốn bao nhiêu token và bao nhiêu lượt sửa?**

## Ma trận hiệu quả cố tình rất bảo thủ

Khi đã có mission, bước tự nhiên tiếp theo là so sánh model theo loại công việc. Nhưng đây cũng là chỗ rất dễ tạo ra một benchmark đẹp mắt mà sai.

Ma trận hiện tại chỉ nhận mission thỏa cả ba điều kiện:

```text
accepted && pure_model && total_tokens > 0
```

Mỗi ô model × loại công việc cần ít nhất 3 mẫu trước khi được coi là đủ mẫu. Nếu chưa từng có dữ liệu, UI hiển thị `Chưa có dữ liệu`. Nếu đã có nhưng chưa đủ, UI hiển thị `Chưa đủ mẫu (n=...)`. Tracker không lấy hệ số của một loại công việc khác rồi nội suy sang ô đang thiếu.

`Sol High` chỉ được dùng làm baseline **trong cùng loại công việc và cùng tập dữ liệu đủ điều kiện**. Nó không phải một tuyên bố rằng Sol High là model tốt nhất nói chung.

## Tự động phân nhóm chỉ là gợi ý

Mission grouping dựa trên heuristic nên sẽ có trường hợp hệ thống đoán sai ranh giới hoặc sai loại công việc. Vì thế dashboard có phần review thủ công để:

- ghép mission với mission trước;
- tách lượt cuối thành mission mới;
- sửa loại công việc;
- xác nhận accepted / unresolved / abandoned;
- trả ranh giới về heuristic tự động.

Các hiệu chỉnh này được lưu cục bộ. Ý tưởng ở đây là automation giúp giảm công kiểm tra, nhưng không được phép biến suy đoán thành “ground truth” chỉ vì nó được sinh tự động.

## Vì sao tổng token theo ngày cũng cần kiểm toán

Một lỗi khác từng xuất hiện khi đối chiếu tổng token theo ngày: một số trường trong log là giá trị cộng dồn. Nếu cộng tất cả `thread_token_usage` hoặc `turn_token_usage` như thể mỗi giá trị là một lần tiêu thụ độc lập, tổng sẽ bị phóng đại.

Quy tắc hiện tại là ưu tiên token ở mức response (`token_usage_record.payload.usage.total_tokens`) hoặc tính delta theo thứ tự thời gian khi dữ liệu là cumulative. Khi có chênh lệch ngày, cần kiểm tra cả ranh giới giờ địa phương, UTC và rolling window trước khi sửa công thức.

## ChatGPT Web và cached input

Usage Tracker cũng cố tránh kết luận quá mức từ telemetry thiếu dữ liệu. Ví dụ, nếu một nguồn ChatGPT Web cho `cached_input_tokens = 0`, điều đó chỉ có nghĩa là tracker không nhận được telemetry cache hữu ích từ nguồn đó; nó **không chứng minh** rằng nền tảng không dùng cache.

Vì vậy chi phí của ChatGPT Web trong tracker được xem là estimate/proxy khi không có nguồn authoritative tương ứng, và không nên so trực tiếp với dữ liệu cache-aware theo cách giả vờ rằng hai phía có cùng mức quan sát.

## Song ngữ Việt / Anh

UI hỗ trợ `Tiếng Việt` và `English` ngay trên header. Lựa chọn ngôn ngữ được lưu trong trình duyệt và phần giao diện động được render lại khi chuyển ngôn ngữ, bao gồm bảng, trạng thái quota, toast, dialog, biểu đồ canvas và các màn hình review mission.

README cũng được viết đầy đủ bằng cả hai ngôn ngữ. Bản tiếng Anh bên dưới là bản diễn giải tự nhiên cùng ý tưởng, không phải bản dịch máy từng câu.

## Nguồn dữ liệu mà tracker có thể đọc

- Codex local sessions/transcripts.
- Codex app-server rate-limit RPC khi local Codex tương thích và đang sẵn sàng.
- Gemini/Antigravity local transcript và account metadata khi phát hiện được.
- Dữ liệu manual/configured chỉ dùng ở những phần có hỗ trợ fallback.

Usage Tracker không cần backend hosted. UI hiện vẫn tải Google Fonts từ public Google Fonts CDN; avatar URL có thể được hiển thị nếu metadata tài khoản cục bộ cung cấp URL đó.

## Cài đặt và chạy nhanh

Yêu cầu hiện tại:

- Windows 10 hoặc Windows 11.
- Python 3. Tracker ưu tiên Python runtime đi kèm Codex nếu tìm thấy, sau đó fallback sang `py -3` hoặc `python`.
- Trình duyệt hiện đại.
- Node.js chỉ cần cho kiểm tra cú pháp JavaScript trong quá trình development/CI.

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
http://127.0.0.1:5050/
```

Dừng hoặc restart server:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop-server.ps1
powershell -ExecutionPolicy Bypass -File .\restart-server.ps1
```

PID và log runtime được ghi dưới `runtime/`.

## Độ chính xác và cách đọc số liệu

Không phải mọi con số trên dashboard đều có cùng độ chắc chắn. Hãy đọc source/provenance đi cùng số liệu:

- **Live / exact**: đọc trực tiếp từ nguồn cục bộ hỗ trợ giá trị đó.
- **Log-derived**: tính từ session/transcript đã ghi trên máy.
- **Estimated / inferred**: suy ra từ quan sát hoặc proxy khi nhà cung cấp không đưa ra số authoritative.
- **Manual/configured**: do người dùng nhập hoặc cấu hình để làm fallback.

Cost estimate và quota-capacity estimate là công cụ phân tích, không phải billing record chính thức. Task-outcome classifier và mission grouping là heuristic; những mục quan trọng nên được review thủ công trước khi dùng làm benchmark.

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

## Development và kiểm thử

Chạy bộ kiểm tra cục bộ:

```powershell
node --check app.js
node --check i18n.js
python -B -m unittest discover -v
python -B -m py_compile server.py run_server.py
git diff --check
```

Các test chính hiện bao phủ nguồn usage, quota estimator, model catalog, Codex task outcomes và Codex app-server rate limits. GitHub Actions chạy các kiểm tra core trên Windows.

## Đóng góp, bảo mật và giấy phép

Xem `CONTRIBUTING.md` để biết quy trình development/pull request và `SECURITY.md` để báo lỗi liên quan bảo mật hoặc quyền riêng tư.

Giấy phép: MIT, xem `LICENSE`.

---

# English

## Why I built Usage Tracker

The first problem was simple: I was using several coding assistants, but the dashboards around them did not reliably answer the questions I actually cared about.

I did not only want a lifetime token counter. I wanted to know:

- How much Codex quota is left in the 5-hour and weekly windows right now?
- Is that number live, derived from logs, or merely estimated?
- If a model used a lot of tokens, did it actually finish the job, or did I have to correct it, teach it again, and keep going?
- If a task switched models or delegated work to a subagent, which model should receive the credit and cost?
- If cost or usage is unknown, can the tracker preserve “unknown” instead of quietly turning it into zero?

Usage Tracker grew out of those questions. That is why this repository is more than a token counter: most of its larger features were added after a real measurement failure or after raw totals stopped being enough to answer the next practical question.

## From quota guessing to a live local source

The earliest versions depended heavily on local sessions and transcripts. Those records were useful for reconstructing what happened, but they were not sufficient for answering the live Codex quota question with confidence.

The tracker therefore evolved in layers:

1. Scan local sessions and normalize records to reduce double counting.
2. Keep the 5-hour and 7-day windows separate instead of collapsing them into one generic “quota” number.
3. When supported by the local Codex installation, read rate limits directly from the Codex app-server.
4. Preserve source attribution so the UI can distinguish live data from session-log fallback.

That distinction came from a real failure mode. A tracker launched from the desktop could sometimes fail to locate the `codex` executable through `PATH`. The app still ran, but it silently fell back to stale session logs, producing numbers that looked plausible while no longer being current.

The project therefore treats the **live app-server as authoritative when it is available, while still supporting a clearly labeled fallback**. A separate browser-cache issue once kept an old `app.js` alive after an update, so the main static assets are version-busted as well.

## Why quota calibration cannot depend on one percentage entry

The next problem was mapping observed token activity to a remaining-quota percentage.

The tempting implementation is to enter one percentage and solve backwards for the full capacity. In practice, IDE percentages are rounded, two readings may belong to different reset cycles, token evidence may come from different sources, and one noisy point can push an estimator in the wrong direction.

The current calibration flow therefore treats each manual percentage as **one observation**, not as ground truth. The estimator combines multiple observations, favors same-cycle pairs, accounts for percentage-rounding bounds, isolates reset boundaries, lets old evidence expire, maintains capacity by source, and retains the prior when new evidence is insufficient.

One observation anchors the estimate; repeated consistent evidence is what earns a change in the estimate.

## Evidence sources must stay separate

Another lesson was that “tokens” do not always mean the same thing operationally. Usage Tracker may encounter:

- transcript-estimated tokens;
- exact tokens from worker/report data;
- automatically scanned Codex session-log tokens;
- manually entered or configured fallback data.

If those are flattened into one unlabeled total, an exact measurement and an estimate become indistinguishable. The tracker therefore preserves provenance in the underlying data and in the UI. The same rule applies to missing values: unknown cost or usage should remain unknown rather than becoming `$0` or `0 tokens` just to fill a table cell.

## Raw tokens still do not tell you whether a model was effective

This became the biggest conceptual change in the project.

Suppose a task begins with one prompt, then requires a correction, a clarification, another implementation pass, and one final fix before the result is accepted. Comparing each turn independently gives a distorted picture. A cheap first turn may still belong to an expensive objective once all repair work is included.

That led to the **mission** model: measure from the beginning of a real objective until that objective is accepted or remains unresolved.

The mission scanner:

- reads both active Codex `sessions` and `archived_sessions`;
- groups related corrections, follow-ups, and continuation turns into one objective;
- preserves model, effort, and route instead of reducing everything to an anonymous token total;
- attaches subagent work to its parent mission when possible;
- marks model-switch or delegated routes as `pure_model=false`, so a mixed route is not falsely credited to one model.

The useful question then becomes: **from the first request until the result was actually accepted, how many tokens and repair turns did this route require?**

## The model-efficiency matrix is deliberately conservative

Once missions existed, comparing models by task type became possible. It also became very easy to build a convincing-looking benchmark from weak evidence, so the matrix is intentionally strict.

Only missions satisfying all of the following are eligible:

```text
accepted && pure_model && total_tokens > 0
```

Each model × task-type cell needs at least 3 samples before it is treated as adequately sampled. A never-observed cell is shown as `No data`. A sparse cell is shown as `Insufficient samples (n=...)`. The tracker does not borrow a coefficient from a different task category to fill the gap.

`Sol High` is used only as a baseline **within the same task category and eligible comparison set**. This is not a claim that Sol High is universally the best model.

## Automatic grouping is a suggestion, not ground truth

Mission boundaries and task categories are heuristic, so the dashboard includes manual review controls for:

- merging with the previous mission;
- splitting the last turn into a new mission;
- changing the task category;
- marking an outcome accepted, unresolved, or abandoned;
- restoring automatic boundary detection.

Those corrections stay local. Automation reduces review work; it does not get to promote a guess into ground truth simply because the guess was generated automatically.

## Even daily token totals needed an audit

During reconciliation work, another source of error appeared: some usage fields are cumulative. Summing every `thread_token_usage` or `turn_token_usage` value as though it were an independent increment can dramatically overstate daily usage.

The current accounting rule is to prefer per-response `token_usage_record.payload.usage.total_tokens`, or to derive chronological deltas when the available source is cumulative. When daily totals disagree, local time, UTC boundaries, and rolling-window boundaries should be checked before changing the accounting logic.

## A note about ChatGPT Web cached-input telemetry

Usage Tracker also tries not to infer more than the source can prove. If a ChatGPT Web source reports `cached_input_tokens = 0`, that means useful cache telemetry is unavailable to this tracker; it does **not** prove that the platform itself did not use caching.

Accordingly, ChatGPT Web cost is treated as an estimate/proxy when an authoritative source is unavailable, and it should not be presented as directly cache-comparable to a cache-aware local source.

## Vietnamese and English UI

The dashboard supports `Tiếng Việt` and `English` from the header. The selected language is persisted in the browser, and dynamic views re-render when the language changes, including tables, quota status, toasts, dialogs, canvas charts, and mission-review screens.

This README also contains complete Vietnamese and English explanations. The English section is written naturally from the same project history rather than generated as a literal line-by-line translation.

## Data sources Usage Tracker can read

- Local Codex sessions/transcripts.
- Codex app-server rate-limit RPC when a compatible local Codex installation is available.
- Local Gemini/Antigravity transcripts and account metadata when detected.
- Manual/configured data only where a fallback path is supported.

Usage Tracker does not require a hosted backend. The UI currently loads Google Fonts from the public Google Fonts CDN, and an account avatar URL may be displayed when one is present in local account metadata.

## Requirements and quick start

Current target:

- Windows 10 or Windows 11.
- Python 3. Usage Tracker prefers a Python runtime bundled with Codex when one is available, then falls back to `py -3` or `python`.
- A modern browser.
- Node.js is optional and is only required for the JavaScript syntax check used during development/CI.

From the repository root:

```powershell
.\start.bat
```

Or start the server without opening a browser automatically:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-background.ps1
```

Then open:

```text
http://127.0.0.1:5050/
```

Stop or restart the local server with:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop-server.ps1
powershell -ExecutionPolicy Bypass -File .\restart-server.ps1
```

Runtime PID and server logs are stored under `runtime/`.

## Accuracy and provenance

Not every number in the dashboard has the same evidentiary strength. Read the source/provenance attached to a value:

- **Live / exact**: read directly from a supported local source.
- **Log-derived**: computed from sessions/transcripts recorded on the machine.
- **Estimated / inferred**: derived from observations or proxies when the provider does not expose an authoritative value.
- **Manual/configured**: entered or configured by the user as a fallback.

Cost estimates and inferred quota capacities are analytical aids, not official billing records. The task-outcome classifier and mission grouping are heuristic; important low-sample results should be manually reviewed before they are treated as benchmarking evidence.

## Local data and privacy

The following files and directories are intentionally excluded by `.gitignore`:

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

They may contain account identifiers, prompts, local paths, usage history, or other private machine data. **Do not use `git add -f` on these files before reviewing them yourself.**

A public repository does not need private projects, personal prompt history, or private trading data in order to run Usage Tracker.

## Development and tests

Run the local validation set with:

```powershell
node --check app.js
node --check i18n.js
python -B -m unittest discover -v
python -B -m py_compile server.py run_server.py
git diff --check
```

The current test suite covers usage sources, quota estimation, the model catalog, Codex task outcomes, and Codex app-server rate-limit handling. GitHub Actions runs the core checks on Windows.

## Contributing, security, and license

See `CONTRIBUTING.md` for development and pull-request guidance. Security and privacy issues should follow `SECURITY.md`.

License: MIT. See `LICENSE`.
