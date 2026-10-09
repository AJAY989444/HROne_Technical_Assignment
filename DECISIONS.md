# DECISIONS.md

1. **Indexes.**
I created three indexes on `attendance_logs`: a unique compound index on `(emp_code, date)` to enforce one record per day per employee and prevent duplicate punch-ins; a descending index on `(emp_code, punch_in)` to quickly locate open punch-ins during punch-out; and a compound index on `(date, emp_code, status)` to accelerate range filters and pagination. On `employees`, I created a unique index on `emp_code` and single/compound indexes on `joined_on` and `(department, joined_on)` to guarantee fast headcount lookups during aggregation. I considered and rejected a standalone index on `status` because status has low cardinality and would add write overhead without selectivity.

2. **Punch-in race.**
When two identical punch-in requests arrive simultaneously, both pass input validation and attempt an `insert_one` into MongoDB. The unique compound index on `(emp_code, date)` allows only the first request to succeed (HTTP 201). The second concurrent request causes a MongoDB `DuplicateKeyError`, which my exception handler catches and converts into HTTP 409 Conflict.

3. **Ties.**
When two employees have identical total late minutes at the cutoff limit, my leaderboard pipeline uses MongoDB's `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4) followed by a secondary tiebreaker sort on `emp_code` ascending. Both tied employees receive rank 2, ensuring fair deterministic ranking without skipping ties.

4. **Headcount.**
The department summary aggregation pipeline starts on the `employees` collection filtered by `joined_on <= month_end`, and then `$lookup` joins matching `attendance_logs`. Starting from the employee master record ensures employees with zero attendance logs are fully included in department headcount and working days calculations.

5. **One thing I would change for 100x data.**
If scaling to 100x data (~10 million records), I would pre-compute and maintain daily and monthly department summary metrics in a dedicated `monthly_analytics` summary collection updated asynchronously via change streams or cron background workers, avoiding on-the-fly multi-collection joins.
