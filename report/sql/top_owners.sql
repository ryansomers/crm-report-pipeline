-- Owners ranked by closed-won value in the report week.
-- RANK() (a window function, portable to SQLite 3.25+) gives ties the same
-- rank, so two reps with equal won value both show as #1.
WITH owner_wins AS (
    SELECT owner, COUNT(*) AS won_count, SUM(amount) AS won_value
    FROM deals
    WHERE stage = 'closed_won'
      AND close_date >= :week_start_date
      AND close_date <  :week_end_date
    GROUP BY owner
)
SELECT
    RANK() OVER (ORDER BY won_value DESC) AS owner_rank,
    owner,
    won_count,
    won_value
FROM owner_wins
ORDER BY owner_rank, owner
LIMIT :top_n;
