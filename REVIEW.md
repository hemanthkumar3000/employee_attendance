# Starter Code Review

| # | Where (function / area) | What was wrong | How to notice | How it was fixed |
|---|---|---|---|---|
| 1 | `list_employees` | Pagination skipped the first page, and the total count did not apply the department filter. | Request the first page with a department filter; the first results are skipped or the total includes employees from other departments. | Applied the department filter to both the query and count, corrected the skip formula, sorted by `emp_code`, and bounded pagination values. |

| 2 | `list_attendance` | Attendance records were fetched and sorted in Python, which could become slow as the database grows. MongoDB's internal `_id` was also included in the response. | Inspect the endpoint's behavior with a larger collection and check the response fields. | Moved filtering, sorting, pagination, and counting to MongoDB and excluded `_id` from returned documents. |

| 3 | `punch_in` | Unknown employees, invalid requests, timestamp conversion, overnight attendance dates, lateness, and concurrent duplicate requests were not handled correctly. | Try an unknown employee, invalid status or timestamp, overnight punch-in, duplicate request, or simultaneous requests for one employee and date. | Added request validation, return 404 for unknown employees, convert and truncate timestamps, calculate the IST attendance date and lateness, and return 409 for `DuplicateKeyError` using the unique index. |

| 4 | Punch-out and manual corrections | These operations were missing, so work duration, overtime, half-day calculations, and correction history were not handled. | Call the contract endpoints and inspect the updated attendance record and its history. | Implemented the operations, added time validation, recalculated derived attendance fields, and recorded manual corrections in history. |

| 5 | Analytics routes | Required monthly summaries, department summaries, leaderboards, trends, and query-explain operations were missing. | Request the analytics and explain paths specified in the OpenAPI contract. | Implemented the endpoints using MongoDB aggregation pipelines and reused pipeline-building logic for explain. |

| 6 | Department trend | A duplicate, unused handler returned an empty `items` array instead of daily attendance data. | Request a valid date range and check the returned number of daily rows. | Removed the unused handler and retained the implementation that generates the date range and daily trend data. |

| 7 | Admin explain and employee indexes | Explain functionality and some indexes were missing. Explain output also had JSON serialization and Atlas trend-query issues, and the department summary's `joined_on` filter needed index support. | Call each explain URL; inspect its response, query plan, and whether the relevant index is used. | Reused query-building logic, converted explain metadata to JSON-compatible data, corrected the trend explain operation, and added an index on `joined_on`. |
