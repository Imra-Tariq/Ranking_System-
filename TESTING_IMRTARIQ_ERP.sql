-- DATA FLOW:
--      INPUT TABLES : temp_emp_data + temp_promotion + temp_reappointment
--      combined_data        (Step 1:  merges all input tables — dates normalized here)
--      all_bps_events       (Step 2:  one row per BPS career event)
--      bps_earliest         (Step 3:  earliest date per employee per BPS level)
--      tiedgroups           (Step 4c: one best record per employee + tied group)
--      seniority_paths      (Step 5:  encodes entire career as one sortable string)
--      emp_base             (Step 6a: personal tiebreaker fields DOEIG/DOB)
--      seniority_tracking   (Step 6b: combines path + personal fields)
--      seniority_final      (Step 7:  compute numeric rank via self-join)
--      max_segments         (Step 8a: Finds how deep the bps career goes)
--      seg_numbers          (Step 8b: Builds a simple counter from 1 to that depth)
--      prefix_group_sizes2  (Step 8c: At each depth, count how many employees share the same history)
--      tiebreak_depth       (Step 8d: Finds the exact depth where each employee became unique)
--      seniority_report     (Step 8e: decision basis — Seniority report stored as table)

-- -------------------------------------------------------------------------------------------------------------------
-- Step #1: Table-1 — combine all three input tables into one row per employee event
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS combined_data;
CREATE TABLE combined_data AS
SELECT
    m.Sno,
    m.ArfNo,
    m.Namee,
    m.Trade,
    m.qualification,
    STR_TO_DATE(m.DateOfBirth,        '%Y-%m-%d') AS DateOfBirth,          -- Tiebreaker #5: older DOB = more senior
    STR_TO_DATE(m.dateofentryingov,   '%Y-%m-%d') AS dateofentryingov,     -- Tiebreaker #4: earlier govt entry = more senior
    STR_TO_DATE(m.DateOfJoining,      '%Y-%m-%d') AS DateOfJoining,        -- Date employee first joined at DateOfJoiningbps grade
    m.DateOfJoiningbps,                                                    -- BPS level at time of joining
    STR_TO_DATE(p.dateofpromotion,    '%Y-%m-%d') AS dateofpromotion,      -- '%d-%b-%Y', actual data is 'YYYY-MM-DD'
    p.dateofpromotionbps,                                                  -- BPS level after promotion
    STR_TO_DATE(r.dateofreappoitment, '%Y-%m-%d') AS dateofreappoitment,   -- Date of reappointment
    r.dateofreappoitmentbps                                                -- BPS level at reappointment

FROM temp_emp_data m
LEFT JOIN temp_promotion     p ON m.ArfNo = p.ArfNo
LEFT JOIN temp_reappointment r ON m.ArfNo = r.ArfNo;

SELECT * FROM combined_data;
-- WHY: The employee master, promotion, and reappointment data live in three separate tables.
--      Everything needs to be in one place, keyed on ArfNo.
-- HOW: LEFT JOIN means every employee in temp_emp_data appears exactly once.
--      Employees with no promotion     get NULL in p.* columns.
--      Employees with no reappointment get NULL in r.* columns.
-- COLUMN-BY-COLUMN FORMAT MAPPING (verified from actual data):
--     m.DateOfBirth        → 'YYYY-MM-DD' → '%Y-%m-%d'
--     m.dateofentryingov   → 'YYYY-MM-DD' → '%Y-%m-%d'
--     m.DateOfJoining      → 'YYYY-MM-DD' → '%Y-%m-%d'
--     p.dateofpromotion    → 'DD-Mon-YYYY' → '%d-%b-%Y'   ← DIFFERENT from the others
--     r.dateofreappoitment → 'YYYY-MM-DD' → '%Y-%m-%d'


-- -------------------------------------------------------------------------------------------------------------------
-- Step #2: Table-2 — union all three BPS event types into one flat list per employee

-- ALL THREE SOURCES FEED THE SENIORITY PATH:
--   SOURCE 1 — Joining      : the BPS level and date the employee first entered service
--   SOURCE 2 — Reappointment: if an employee left and rejoined, this captures that BPS event
--   SOURCE 3 — Promotion    : every upward BPS move via promotion
--
--   These three UNION ALL blocks are the single entry point for all career BPS data.
--   Every downstream table (bps_earliest → seniority_paths → seniority_final) is built
--   from this combined list, so the seniority path always reflects all three sources.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS all_bps_events;
CREATE TABLE all_bps_events AS
SELECT
    ArfNo,
    MAX(Namee) AS Namee,
    bps_level,
    event_date,
    source
FROM (

    -- SOURCE 1: Joining event
    SELECT
        ArfNo,
        Namee,
        CAST(DateOfJoiningbps AS SIGNED) AS bps_level,
        DateOfJoining                    AS event_date,
        'Joining'                        AS source
    FROM combined_data
    WHERE DateOfJoining    IS NOT NULL
      AND DateOfJoiningbps IS NOT NULL

    UNION ALL

    -- SOURCE 2: Reappointment event
    SELECT
        ArfNo,
        Namee,
        CAST(dateofreappoitmentbps AS SIGNED),
        dateofreappoitment,
        'Reappointment'
    FROM combined_data
    WHERE dateofreappoitment    IS NOT NULL
      AND dateofreappoitmentbps IS NOT NULL

    UNION ALL

    -- SOURCE 3: Promotion event
    SELECT
        ArfNo,
        Namee,
        CAST(dateofpromotionbps AS SIGNED),
        dateofpromotion,
        'Promotion'
    FROM combined_data
    WHERE dateofpromotion    IS NOT NULL
      AND dateofpromotionbps IS NOT NULL

) raw_events
GROUP BY ArfNo, bps_level, event_date, source;

SELECT ArfNo, Namee, bps_level, event_date, source
FROM all_bps_events
ORDER BY ArfNo, bps_level, event_date;




-- WHY: An employee's seniority depends on ALL the BPS levels they have ever reached
--      and WHEN they reached each one — regardless of whether it was via joining,
--      promotion, or reappointment. We need a uniform flat list.
-- HOW: Three SELECT blocks, one per event type, stacked with UNION ALL.
--      UNION ALL (not UNION) keeps all rows — we want every event.


-- -------------------------------------------------------------------------------------------------------------------
-- Step #3: Table-3 — earliest date each employee reached each BPS level
--          across ALL three sources (Joining + Reappointment + Promotion)
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS bps_earliest;
CREATE TABLE bps_earliest AS
SELECT
    ArfNo,
    bps_level,
    MIN(event_date) AS achieved_date    -- MIN across all sources: whichever came first
FROM all_bps_events
GROUP BY ArfNo, bps_level;

-- WHY: An employee might reach the same BPS level via two different sources
--      (e.g. reappointed at the same grade they had previously been promoted to).
--      MIN(event_date) ensures we always use the earliest date regardless of source.
--      This table feeds Step 5 (seniority_paths) directly — so the path is always built
--      from the earliest possible date at each BPS level, drawn from all three sources.

-- Step 3b: one entry per employee showing their highest BPS
SELECT
    ArfNo,
    MAX(Namee)                     AS Namee,
    MAX(CAST(bps_level AS SIGNED)) AS highest_bps
FROM all_bps_events
GROUP BY ArfNo
ORDER BY ArfNo;


-- -------------------------------------------------------------------------------------------------------------------
-- Step #4: Counts of events at each BPS level
-- -------------------------------------------------------------------------------------------------------------------
SELECT
    bps_level,
    COUNT(*) AS total_count
FROM all_bps_events
GROUP BY bps_level
ORDER BY bps_level;

-- Step 4b: employees behind each count
SELECT
    bps_level,
    GROUP_CONCAT(
        CONCAT(ArfNo, '-', Namee, ' (', source, ')')
        ORDER BY event_date
        SEPARATOR '\n'
    ) AS employees
FROM all_bps_events
GROUP BY bps_level
ORDER BY bps_level;

-- Step 4c: Table-4 — one row per employee: highest BPS + earliest date at that peak
DROP TABLE IF EXISTS tiedgroups;
CREATE TABLE tiedgroups AS
SELECT
    e.ArfNo,
    MAX(e.Namee)                     AS Namee,
    MAX(CAST(e.bps_level AS SIGNED)) AS bps_level,
    MIN(e.event_date)                AS event_date
FROM all_bps_events e
INNER JOIN (
    SELECT ArfNo, MAX(CAST(bps_level AS SIGNED)) AS max_bps
    FROM all_bps_events
    GROUP BY ArfNo
) mb ON e.ArfNo = mb.ArfNo
    AND CAST(e.bps_level AS SIGNED) = mb.max_bps
GROUP BY e.ArfNo;

-- Step 4d: seniority rank with tied-group display
SELECT
    a.ArfNo,
    a.Namee,
    a.bps_level  AS highest_bps,
    a.event_date AS achieved_date,
    1 + COUNT(b.ArfNo) AS seniority_rank,
    (
        SELECT GROUP_CONCAT(t.ArfNo, '-', t.Namee ORDER BY t.ArfNo SEPARATOR ' | ')
        FROM tiedgroups t
        WHERE t.bps_level  = a.bps_level
          AND t.event_date = a.event_date
          AND t.ArfNo     != a.ArfNo
    ) AS tied_with
FROM tiedgroups a
LEFT JOIN tiedgroups b
    ON (
        b.bps_level > a.bps_level
        OR (    b.bps_level  = a.bps_level
            AND b.event_date < a.event_date)
    )
GROUP BY a.ArfNo, a.Namee, a.bps_level, a.event_date
ORDER BY seniority_rank;


-- -------------------------------------------------------------------------------------------------------------------
-- STEP 5: Table-5 — build the SENIORITY PATH — one sortable string per employee
--
-- INPUT: bps_earliest — which already contains the MIN(event_date) per BPS level
--        drawn from all three sources (Joining + Reappointment + Promotion).
--        So every segment encoded here already reflects all three sources.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS seniority_paths;
CREATE TABLE seniority_paths AS
SELECT
    ArfNo,

    MAX(bps_level) AS highest_bps,                  -- Tier 1 tiebreaker: the employee's peak BPS level

    -- TIER 2: Earliest date the peak BPS was reached (from any source)
    MIN(CASE
            WHEN bps_level = (
                SELECT MAX(bps_level)
                FROM bps_earliest s2
                WHERE s2.ArfNo = bps_earliest.ArfNo
            )
            THEN achieved_date
        END
    ) AS highest_bps_date,

    -- TIER 3: Full sortable career history string
    -- Each segment = LPAD(100 - bps_level, 2, '0') + '-' + DATE_FORMAT(achieved_date, '%Y%m%d')
    --              = 2 chars + 1 char + 8 chars = exactly 11 chars
    -- Segments separated by '_' → N segments = 12N-1 total characters
    GROUP_CONCAT(
        CONCAT(
            LPAD(100 - bps_level, 2, '0'),           -- Inverted BPS prefix: higher BPS → smaller number → sorts first
            '-',
            DATE_FORMAT(achieved_date, '%Y%m%d')     -- YYYYMMDD: earlier date sorts first (more senior)
        )
        ORDER BY bps_level DESC                      -- CRITICAL: highest BPS must appear FIRST in the string
        SEPARATOR '_'
    ) AS seniority_path

FROM bps_earliest
GROUP BY ArfNo;

-- THE ENCODING TRICK:
--   BPS 22 → 100-22 = 78 → '78'   BPS 5 → 100-5 = 95 → '95'
--   '78' < '95'    higher BPS sorts before lower BPS
--   '20050601' < '20100315' as string = same as chronologically 
--
-- EXAMPLE — BPS history: 22 (2010-03-15), 17 (2005-06-01), 14 (2001-01-01)
--   Path: '78-20100315_83-20050601_86-20010101'
--
-- FIXED-LENGTH GUARANTEE:
--   Every segment = exactly 11 chars. Every separator = 1 char.
--   N segments = 12N-1 characters total. This lets Step 8e extract any segment by position.


-- -------------------------------------------------------------------------------------------------------------------
-- STEP 6a: Table-6a EMP_BASE— one row per employee with personal tiebreaker fields
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS emp_base;
CREATE TABLE emp_base AS
SELECT
    ArfNo,
    MAX(Namee)            AS Namee,
    MIN(dateofentryingov) AS dateofentryingov,
    MIN(DateOfBirth)      AS DateOfBirth
FROM combined_data
GROUP BY ArfNo;

-- WHY: combined_data used LEFT JOINs so an employee with both a promotion and a
--      reappointment appears on TWO rows. GROUP BY collapses to one row per employee.


-- -------------------------------------------------------------------------------------------------------------------
-- Step #6b: TABLE 6-b seniority_tracking — BPS path + personal tiebreakers in one row per employee
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS seniority_tracking;
CREATE TABLE seniority_tracking AS
SELECT
    p.ArfNo,
    b.Namee,
    b.dateofentryingov,
    b.DateOfBirth,
    p.highest_bps,
    p.highest_bps_date,
    p.seniority_path
FROM seniority_paths p
JOIN emp_base b ON p.ArfNo = b.ArfNo;

-- WHY: Step 5 produced the BPS career path. Step 6a produced the personal fields.
--      INNER JOIN is safe — both tables are derived from the same employee population.


-- -------------------------------------------------------------------------------------------------------------------
-- STEP 7: Table-7 ( seniority_final) — compute numeric seniority rank for every employee via self-join
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS seniority_final;
CREATE TABLE seniority_final AS
SELECT
    a.ArfNo,
    a.Namee,
    a.highest_bps,
    a.highest_bps_date,
    a.dateofentryingov,
    a.DateOfBirth,
    a.seniority_path,
    1 + COUNT(b.ArfNo) AS seniority_rank
FROM seniority_tracking a
LEFT JOIN seniority_tracking b
    ON (
        -- TIER 1+2+3: Pad both paths to 500 chars with '~' before comparing.
        -- This ensures a shorter path (fewer BPS events) never incorrectly
        -- beats a longer path (more BPS events) when all shared segments are equal.
        -- An employee with a deeper earlier career history is always ranked more senior.
        RPAD(b.seniority_path, 500, '~') < RPAD(a.seniority_path, 500, '~')

        -- TIER 4: Same career path, but B entered government earlier
        OR (    b.seniority_path   =  a.seniority_path
            AND b.dateofentryingov <  a.dateofentryingov )

        -- TIER 5: Same path + same entry date, but B was born earlier
        OR (    b.seniority_path   =  a.seniority_path
            AND b.dateofentryingov =  a.dateofentryingov
            AND b.DateOfBirth      <  a.DateOfBirth      )

        -- TIER 6: Same path + same entry date + same DOB, but B has a smaller ArfNo
        OR (    b.seniority_path   =  a.seniority_path
            AND b.dateofentryingov =  a.dateofentryingov
            AND b.DateOfBirth      =  a.DateOfBirth
            AND b.ArfNo            <  a.ArfNo            )
    )
GROUP BY
    a.ArfNo, a.Namee,
    a.highest_bps, a.highest_bps_date,
    a.dateofentryingov, a.DateOfBirth,
    a.seniority_path;

-- METHOD — counting-based ranking:
--   Rank of A = 1 + (number of employees B strictly more senior than A).
--   LEFT JOIN keeps rank-1 employees (no B matches → COUNT = 0 → rank = 1).
--   COUNT(b.ArfNo) not COUNT(*): avoids counting the NULL row for rank-1 employees.

--   THE BUG WITHOUT PADDING:
--     Employee A path: '85-20201230_86-20180228_87-20070519'              (3 segments, 35 chars)
--     Employee B path: '85-20201230_86-20180228_87-20070519_88-19950610'  (4 segments, 47 chars)
--     MySQL string comparison stops at position 36 where A's string ends.
--     An ended (shorter) string is treated as SMALLER → A ranks above B.
--     This is WRONG: B has an earlier BPS-12 from 1995 and should be MORE senior.
--
--   THE FIX — RPAD with '~' (tilde, ASCII 126):
--     '~' has a higher ASCII value than all real path characters:
--       digits      : ASCII  48– 57  ('0'–'9')
--       hyphen      : ASCII  45      ('-')
--       underscore  : ASCII  95      ('_')
--       letters     : ASCII  65–122  ('A'–'z')
--     Padding the shorter path with '~' fills its "missing" segment positions
--     with a character that sorts AFTER any real segment character.
--     After RPAD to 500 chars:
--       A: '85-20201230_86-20180228_87-20070519~~~~~~~~~~~~~~~~~~~~...'
--       B: '85-20201230_86-20180228_87-20070519_88-19950610~~~~~~~~...'
--     At position 36: A has '~' (ASCII 126), B has '_' (ASCII 95).
--     '_' < '~'  →  B's path is smaller  →  B is more senior. ✓
--     Employee with DEEPER earlier career history correctly ranks higher.
--   WHY 500?
--     Generous upper bound well beyond any realistic path length.
--     Max realistic: 10 segments × 12 chars − 1 = 119 chars. 500 is safe.






-- -------------------------------------------------------------------------------------------------------------------
-- STEP 8a — max_segments: finding the maximum number of BPS segments any single employee has
-- PURPOSE: Find out how many BPS segments the most complex employee career has.
--          This tells us how deep we need to scan when looking for where ties break.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS max_segments;
CREATE TABLE max_segments AS
SELECT
    MAX(
        1 + LENGTH(seniority_path) - LENGTH(REPLACE(seniority_path, '_', ''))
        -- Underscores = segments - 1  →  segments = 1 + underscore count
    ) AS max_seg
FROM seniority_final;

-- WHY: We need to know how many depth levels to generate in Step 8b.
--      An employee with 3 BPS events has a path with 3 segments.
--      An employee with 1 BPS event has a path with 1 segment.
--      We need to scan up to the deepest history that exists.
-- HOW — counting underscores:
--   Number of underscores = LENGTH(path) - LENGTH(path with underscores removed)
--   Number of segments    = number of underscores + 1
-- EXAMPLE:
--   path = '78-20100315_83-20050601_86-20010101'
--   LENGTH(path) = 35
--   LENGTH(REPLACE(path, '_', '')) = 33   (2 underscores removed)
--   Segments = 1 + 35 - 33 = 3



-- -------------------------------------------------------------------------------------------------------------------
-- STEP 8b — seg_numbers
-- Building a simple integer sequence table: 1, 2, 3, ... up to max_seg.
-- PURPOSE: This acts as a "loop counter" — MySQL has no built-in sequence generator.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS seg_numbers;
CREATE TABLE seg_numbers AS
SELECT n
FROM (
    SELECT  1 AS n UNION ALL SELECT  2 UNION ALL SELECT  3 UNION ALL SELECT  4 UNION ALL
    SELECT  5       UNION ALL SELECT  6 UNION ALL SELECT  7 UNION ALL SELECT  8 UNION ALL
    SELECT  9       UNION ALL SELECT 10
) nums
WHERE n <= (SELECT max_seg FROM max_segments);


-- WHY: MySQL (unlike PostgreSQL) has no built-in sequence/range generator.
--      We need one integer per depth level to CROSS JOIN against employees in Step 8c.
--      This acts as the "loop counter" in a language that has no loops.
-- LIMITATION: This manually lists integers 1 through 10.
--      If any employee has more than 10 BPS events in their career, extend the UNION ALL chain.
--      The WHERE filter ensures we never generate more rows than actually needed.



-- -------------------------------------------------------------------------------------------------------------------
-- STEP 8c — prefix_group_sizes2
-- For every (employee, depth N): extract the N-segment prefix and count how many
-- employees share that exact prefix.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS prefix_group_sizes2;
CREATE TABLE prefix_group_sizes2 AS
SELECT
    p.ArfNo,
    p.seg_depth,
    p.path_prefix,
    grp.cnt
FROM (
    -- One row per (employee × depth): extract prefix of length 12N-1
    SELECT
        f.ArfNo,
        n.n                              AS seg_depth,
        LEFT(f.seniority_path, n.n*12-1) AS path_prefix
        -- PREFIX LENGTH: N segments × 11 chars + (N-1) separators = 12N-1 chars
        -- N=1→11, N=2→23, N=3→35, N=4→47, N=5→59 ...
    FROM seniority_final f
    CROSS JOIN seg_numbers n
    WHERE n.n <= 1 + LENGTH(f.seniority_path)
                    - LENGTH(REPLACE(f.seniority_path, '_', ''))
    -- Only generate depth-N rows for employees who actually have N segments
) p
JOIN (
    -- For each unique prefix string: count how many employees share it
    SELECT
        LEFT(f.seniority_path, n.n*12-1) AS path_prefix,
        COUNT(*)                          AS cnt
    FROM seniority_final f
    CROSS JOIN seg_numbers n
    WHERE n.n <= 1 + LENGTH(f.seniority_path)
                    - LENGTH(REPLACE(f.seniority_path, '_', ''))
    GROUP BY LEFT(f.seniority_path, n.n*12-1)
) grp ON grp.path_prefix = p.path_prefix;

-- WHY: To find WHERE in an employee's career their rank separated from others,
--      we look at progressively longer prefixes of their seniority path.
--
--      At depth 1: the prefix is just the first segment (highest BPS + its date).
--      If cnt=1 → they are the only employee with that BPS+date → rank resolved at depth 1.
--      If cnt=5 → 5 employees share the same highest BPS + date → need to go deeper.
--
--      At depth 2: prefix includes the 2nd-highest BPS event too.
--        If cnt=1 → tie broke at this level.
--        If cnt>1 → go deeper.
--
--      The smallest depth where cnt=1 is WHERE the ranking decision was made.
--
-- PREFIX LENGTH FORMULA:
--   Each segment = 11 chars (2 + 1 + 8)
--   Each separator = 1 char
--   N segments total = 11*N + (N-1)*1 = 12*N - 1 characters
--   N=1 → 11 chars   N=2 → 23 chars   N=3 → 35 chars  (etc.)
--   So LEFT(path, N*12 - 1) extracts exactly the first N segments. ✓





-- -------------------------------------------------------------------------------------------------------------------
-- STEP 8d — tiebreak_depth
-- PURPOSE: For each employee, find the SMALLEST depth at which they become unique (cnt = 1).
--          This is the segment where their rank was finally decided.
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS tiebreak_depth;
CREATE TABLE tiebreak_depth AS
SELECT
    ArfNo,
    MIN(seg_depth) AS break_depth
FROM prefix_group_sizes2
WHERE cnt = 1
GROUP BY ArfNo;

-- break_depth=1    → peak BPS level or its date decided it
-- break_depth=2    → second-highest BPS level decided it
-- break_depth=NULL → entire BPS path is identical to another employee; Tiers 4/5/6 decide



-- -------------------------------------------------------------------------------------------------------------------
-- Step #8e: FINAL OUTPUT — seniority report stored as a table
-- -------------------------------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS seniority_report;
CREATE TABLE seniority_report AS
SELECT
    f.seniority_rank,
    f.ArfNo,
    f.Namee,
    f.highest_bps,
    f.highest_bps_date  AS date_achieved,
    f.dateofentryingov,
    f.DateOfBirth,
    f.seniority_path,

    -- -----------------------------------------------------------------------
    -- READABLE seniority path — decodes ALL segments generically
    --
    -- No hardcoded segment count. Works for any number of BPS career events.
    -- Uses seg_numbers (Step 8b) as the loop counter via a correlated subquery.
    --
    -- For each segment N that exists in this employee's path:
    --   Position in path string : (N-1)*12 + 1   (1-based, MySQL SUBSTRING)
    --   Segment length          : always 11 chars
    --   Segment format          : "PP-YYYYMMDD"
    --     PP       = SUBSTRING_INDEX(segment, '-',  1) → inverted BPS prefix
    --     YYYYMMDD = SUBSTRING_INDEX(segment, '-', -1) → date string
    --   Recover BPS  : 100 - CAST(PP AS SIGNED)
    --   Recover date : STR_TO_DATE(YYYYMMDD, '%Y%m%d') → DATE_FORMAT → 'DD-Mon-YYYY'
    --
    -- GROUP_CONCAT with ORDER BY n.n keeps segments in BPS-descending order.
    -- The WHERE clause (underscore-count trick) stops the loop at the correct
    -- depth for each individual employee — shorter careers get fewer segments.
    -- -----------------------------------------------------------------------
    (
        SELECT GROUP_CONCAT(
            CONCAT(
                'BPS-',
                100 - CAST(
                    SUBSTRING_INDEX(
                        SUBSTRING(f.seniority_path, (n.n - 1) * 12 + 1, 11),
                        '-', 1
                    ) AS SIGNED
                ),
                ': ',
                DATE_FORMAT(
                    STR_TO_DATE(
                        SUBSTRING_INDEX(
                            SUBSTRING(f.seniority_path, (n.n - 1) * 12 + 1, 11),
                            '-', -1
                        ),
                        '%Y%m%d'
                    ),
                    '%d-%b-%Y'
                )
            )
            ORDER BY n.n                    -- segment 1 first (highest BPS), then descending
            SEPARATOR ' | '
        )
        FROM seg_numbers n
        WHERE n.n <= 1 + LENGTH(f.seniority_path)
                        - LENGTH(REPLACE(f.seniority_path, '_', ''))
        -- Stops at exactly the number of segments this employee has.
        -- An employee with 2 BPS events gets 2 decoded segments.
        -- An employee with 7 BPS events gets 7 decoded segments.
    ) AS seniority_path_readable,

    CASE

        -- CASE 1: Peak BPS is unique — no other employee has ever reached this BPS level
        WHEN td.break_depth = 1
             AND grp_bps.cnt = 1
            THEN CONCAT('Unique at BPS ', f.highest_bps)

        -- CASE 2: Shared peak BPS, but the date at that peak is unique
        WHEN td.break_depth = 1
            THEN CONCAT(
                'Broke at L1 BPS ', f.highest_bps,
                ' date (', DATE_FORMAT(f.highest_bps_date, '%d-%b-%Y'), ')'
            )

        -- CASE 3: Top BPS and date matched others; a deeper career segment was decisive
        WHEN td.break_depth IS NOT NULL
            THEN CONCAT(
                'Broke at L', td.break_depth,
                ' BPS ',
                100 - CAST(
                    SUBSTRING_INDEX(
                        SUBSTRING(f.seniority_path, (td.break_depth - 1) * 12 + 1, 11),
                        '-', 1
                    ) AS SIGNED
                ),
                ' date (',
                DATE_FORMAT(
                    STR_TO_DATE(
                        SUBSTRING_INDEX(
                            SUBSTRING(f.seniority_path, (td.break_depth - 1) * 12 + 1, 11),
                            '-', -1
                        ),
                        '%Y%m%d'
                    ),
                    '%d-%b-%Y'
                ),
                ')'
            )

        -- CASE 4: Identical BPS path — government entry date decided
        WHEN grp_entry.cnt = 1
            THEN CONCAT(
                'Broke by govt entry date (',
                DATE_FORMAT(f.dateofentryingov, '%d-%b-%Y'),
                ')'
            )

        -- CASE 5: Identical path + identical entry date — date of birth decided
        WHEN grp_dob.cnt = 1
            THEN CONCAT(
                'Broke by date of birth (',
                DATE_FORMAT(f.DateOfBirth, '%d-%b-%Y'),
                ')'
            )

        -- CASE 6: Absolute final fallback — smallest ArfNo is most senior
        ELSE CONCAT('Broke by ArfNo (', f.ArfNo, ')')

    END AS decision_basis

FROM seniority_final f

-- JOIN 1: at which path depth this employee became unique
LEFT JOIN tiebreak_depth td
    ON td.ArfNo = f.ArfNo

-- JOIN 2: how many employees share this employee's peak BPS level
--         (distinguishes Case 1 — unique BPS — from Case 2 — shared BPS unique date)
LEFT JOIN (
    SELECT highest_bps, COUNT(*) AS cnt
    FROM seniority_final
    GROUP BY highest_bps
) grp_bps ON grp_bps.highest_bps = f.highest_bps

-- JOIN 3: how many employees share the same path + govt entry date (Case 4)
LEFT JOIN (
    SELECT seniority_path, dateofentryingov, COUNT(*) AS cnt
    FROM seniority_final
    GROUP BY seniority_path, dateofentryingov
) grp_entry ON  grp_entry.seniority_path   = f.seniority_path
           AND  grp_entry.dateofentryingov  = f.dateofentryingov

-- JOIN 4: how many employees share the same path + entry date + DOB (Case 5 vs Case 6)
LEFT JOIN (
    SELECT seniority_path, dateofentryingov, DateOfBirth, COUNT(*) AS cnt
    FROM seniority_final
    GROUP BY seniority_path, dateofentryingov, DateOfBirth
) grp_dob ON  grp_dob.seniority_path   = f.seniority_path
         AND  grp_dob.dateofentryingov  = f.dateofentryingov
         AND  grp_dob.DateOfBirth       = f.DateOfBirth

ORDER BY f.seniority_rank;

-- Read the final report
SELECT * FROM seniority_report ORDER BY seniority_rank;

-- WHY: seniority_path         → raw encoded string, kept for debugging.
--      seniority_path_readable → "BPS-22: 15-Mar-2010 | BPS-17: 01-Jun-2005 | ..."
--                                generic: works for any number of segments automatically.
--      decision_basis          → explains which tiebreaker decided each employee's rank.
--
-- READABLE PATH — SEGMENT POSITION REFERENCE:
--   Seg N : start position = (N-1)*12 + 1,  always 11 chars long
--   Seg 1 : start=  1   Seg 2 : start= 13   Seg 3 : start= 25
--   Seg 4 : start= 37   Seg 5 : start= 49   Seg 6 : start= 61  ...
--
-- THE SIX DECISION CASES:
--   Case 1 : Unique BPS level              → "Unique at BPS 22"
--   Case 2 : Shared BPS, unique date L1    → "Broke at L1 BPS 22 date (15-Mar-2010)"
--   Case 3 : Broke at depth N > 1          → "Broke at L3 BPS 17 date (01-Jun-2005)"
--   Case 4 : Identical path, unique entry  → "Broke by govt entry date (01-Jan-2000)"
--   Case 5 : Identical path+entry, DOB     → "Broke by date of birth (12-Apr-1965)"
--   Case 6 : Everything identical, ArfNo   → "Broke by ArfNo (smallest is most senior)"
--
-- RPAD (Step 7):
--   Without RPAD: shorter path string < longer path string when all shared segments equal.
--   Result was: employee with FEWER BPS events incorrectly ranked MORE senior.
--   With RPAD('~', 500): missing segments fill with '~' (ASCII 126, highest sort value).
--   Result is:  employee with MORE/EARLIER BPS history correctly ranked MORE senior. ✓


-- ============================================================================================================================
-- DROPPING TABLES (uncomment when needed)
-- ============================================================================================================================
/*
 DROP TABLE IF EXISTS combined_data;
 DROP TABLE IF EXISTS all_bps_events;]
 DROP TABLE IF EXISTS bps_earliest;
 DROP TABLE IF EXISTS tiedgroups;
 DROP TABLE IF EXISTS seniority_paths;
 DROP TABLE IF EXISTS emp_base;
 DROP TABLE IF EXISTS seniority_tracking;
 DROP TABLE IF EXISTS seniority_final;
 DROP TABLE IF EXISTS max_segments;
 DROP TABLE IF EXISTS seg_numbers;
 DROP TABLE IF EXISTS prefix_group_sizes2;
 DROP TABLE IF EXISTS tiebreak_depth;
 DROP TABLE IF EXISTS seniority_report;
 */ 
 