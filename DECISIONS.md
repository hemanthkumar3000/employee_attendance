# Design Decisions

I created a unique index on `employees.emp_code` because every employee needs a unique ID. I also added a unique compound index on `attendance_logs.emp_code` and `date` to make sure an employee cannot have multiple attendance records for the same day. The other indexes help the API find employees and attendance records faster when filtering by date, status, department, or joining date. I avoided adding unnecessary indexes because they take up storage and can slow down write operations.

If two punch-in requests come in at the same time, both might check the database and find no existing attendance record. However, the unique index ensures that only one request can insert the record. The other request gets a `DuplicateKeyError`, which the API handles by returning HTTP 409 to indicate a conflict.

For the leaderboard, I used MongoDB's `$rank` to rank employees based on their total late minutes. Employees with the same total get the same rank. The API then returns everyone whose rank falls within the requested limit, including employees tied at the cutoff.

For the department summary, I started with the list of eligible employees and then looked up their attendance records. This way, employees who have no attendance records are still included in the department's headcount.

If the database grows to 100 times its current size, I would first check query performance using execution statistics to identify the slow parts. Based on those results, I would optimize the queries or indexes. If generating analytics became too expensive, I would consider storing precomputed summaries instead of recalculating everything for every request.