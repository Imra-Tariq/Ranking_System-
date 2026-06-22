-- EXECUTION ORDER:
   CALL sp_build_combined_data();
   CALL sp_build_all_bps_events();
   CALL sp_build_bps_earliest();
   CALL sp_build_tiedgroups();
   CALL sp_build_seniority_paths();
   CALL sp_build_emp_base();
   CALL sp_build_seniority_tracking();
   CALL sp_build_seniority_final();
   CALL sp_build_max_segments();
   CALL sp_build_seg_numbers();
   CALL sp_build_prefix_group_sizes2();
   CALL sp_build_tiebreak_depth();
   CALL sp_build_seniority_report();

--    OR run everything at once:
    CALL sp_run_all_seniority();
-- ============================================================================================================================




-- ============================================================================================================================
-- Step 1 — combined_data
-- Merges temp_emp_data + temp_promotion + temp_reappointment into one row per employee event.
-- Normalises all date columns to DATE type during the merge.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_combined_data$$

CREATE PROCEDURE sp_build_combined_data()
BEGIN
    DROP TABLE IF EXISTS combined_data;
    CREATE TABLE combined_data AS
    SELECT
        m.Sno,
        m.ArfNo,
        m.Namee,
        m.Trade,
        m.qualification,
        STR_TO_DATE(m.DateOfBirth,        '%Y-%m-%d') AS DateOfBirth,
        STR_TO_DATE(m.dateofentryingov,   '%Y-%m-%d') AS dateofentryingov,
        STR_TO_DATE(m.DateOfJoining,      '%Y-%m-%d') AS DateOfJoining,
        m.DateOfJoiningbps,
        STR_TO_DATE(p.dateofpromotion,    '%Y-%m-%d') AS dateofpromotion,
        p.dateofpromotionbps,
        STR_TO_DATE(r.dateofreappoitment, '%Y-%m-%d') AS dateofreappoitment,
        r.dateofreappoitmentbps
    FROM temp_emp_data m
    LEFT JOIN temp_promotion     p ON m.ArfNo = p.ArfNo
    LEFT JOIN temp_reappointment r ON m.ArfNo = r.ArfNo;

    SELECT * FROM combined_data;
END$$
DELIMITER ;




-- ============================================================================================================================
-- Step 2 — all_bps_events
-- Unions all three BPS event types (Joining / Reappointment / Promotion) into one flat
-- list per employee.  Every downstream seniority table is derived from this union.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_all_bps_events$$
CREATE PROCEDURE sp_build_all_bps_events()
BEGIN
    DROP TABLE IF EXISTS all_bps_events;
    CREATE TABLE all_bps_events AS
    SELECT
        ArfNo,
        MAX(Namee)   AS Namee,
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

    SELECT * FROM all_bps_events ORDER BY ArfNo, bps_level, event_date;
END$$


-- ============================================================================================================================
-- Step 3 — bps_earliest
-- Earliest date each employee reached each BPS level, across ALL three sources.
-- Also shows each employee's highest BPS.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_bps_earliest$$
CREATE PROCEDURE sp_build_bps_earliest()
BEGIN
    -- Step 3a: earliest date per employee per BPS level
    DROP TABLE IF EXISTS bps_earliest;
    CREATE TABLE bps_earliest AS
    SELECT
        ArfNo,
        bps_level,
        MIN(event_date) AS achieved_date   -- MIN across all sources: whichever came first
    FROM   all_bps_events
    GROUP BY ArfNo, bps_level;

    -- Step 3b: one entry per employee showing their highest BPS (informational)
    SELECT
        ArfNo,
        MAX(Namee)                     AS Namee,
        MAX(CAST(bps_level AS SIGNED)) AS highest_bps
    FROM   all_bps_events
    GROUP BY ArfNo
    ORDER BY ArfNo;

    -- Step 3c: full bps_earliest table
    SELECT * FROM bps_earliest ORDER BY ArfNo, bps_level;
END$$


-- ============================================================================================================================
-- Step 4 — tiedgroups  (plus diagnostic SELECTs)
-- Counts events at each BPS level, lists employees per level, then builds
-- tiedgroups: one row per employee with their highest BPS and earliest date at that peak.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_tiedgroups$$
CREATE PROCEDURE sp_build_tiedgroups()
BEGIN
    -- Step 4a: count of events at each BPS level
    SELECT
        bps_level,
        COUNT(*) AS total_count
    FROM   all_bps_events
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
    FROM   all_bps_events
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
        FROM   all_bps_events
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
            FROM   tiedgroups t
            WHERE  t.bps_level  = a.bps_level
              AND  t.event_date = a.event_date
              AND  t.ArfNo     != a.ArfNo
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

    -- Step 4e: full tiedgroups table
    SELECT * FROM tiedgroups ORDER BY bps_level DESC, event_date;
END$$


-- ============================================================================================================================
-- Step 5 — seniority_paths
-- Encodes each employee's entire BPS career as one sortable string.
-- Higher BPS → smaller prefix so it sorts first.  Earlier date sorts first within same BPS.
-- Each segment = LPAD(100-bps,2,'0') + '-' + YYYYMMDD = exactly 11 chars.
-- Segments separated by '_'.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_seniority_paths$$
CREATE PROCEDURE sp_build_seniority_paths()
BEGIN
    DROP TABLE IF EXISTS seniority_paths;
    CREATE TABLE seniority_paths AS
    SELECT
        ArfNo,
        MAX(bps_level)  AS highest_bps,          -- Tier 1 tiebreaker: the employee's peak BPS level

        -- TIER 2: Earliest date the peak BPS was reached (from any source)
        MIN(CASE
                WHEN bps_level = (
                    SELECT MAX(bps_level)
                    FROM   bps_earliest s2
                    WHERE  s2.ArfNo = bps_earliest.ArfNo
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
                LPAD(100 - bps_level, 2, '0'),          -- Inverted BPS prefix: higher BPS → smaller number → sorts first
                '-',
                DATE_FORMAT(achieved_date, '%Y%m%d')    -- YYYYMMDD: earlier date sorts first (more senior)
            )
            ORDER BY bps_level DESC                     -- CRITICAL: highest BPS must appear FIRST in the string
            SEPARATOR '_'
        ) AS seniority_path

    FROM  bps_earliest
    GROUP BY ArfNo;

    SELECT * FROM seniority_paths ORDER BY highest_bps DESC, highest_bps_date;
END$$


-- ============================================================================================================================
-- Step 6a — emp_base
-- One row per employee with personal tiebreaker fields (entry date, DOB).
-- GROUP BY collapses duplicate rows caused by LEFT JOINs in combined_data.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_emp_base$$
CREATE PROCEDURE sp_build_emp_base()
BEGIN
    DROP TABLE IF EXISTS emp_base;
    CREATE TABLE emp_base AS
    SELECT
        ArfNo,
        MAX(Namee)            AS Namee,
        MIN(dateofentryingov) AS dateofentryingov,
        MIN(DateOfBirth)      AS DateOfBirth
    FROM   combined_data
    GROUP BY ArfNo;

    SELECT * FROM emp_base ORDER BY ArfNo;
END$$


-- ============================================================================================================================
-- Step 6b — seniority_tracking
-- Combines the BPS career path (Step 5) with personal tiebreaker fields (Step 6a).
-- One row per employee, ready for self-join ranking in Step 7.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_seniority_tracking$$
CREATE PROCEDURE sp_build_seniority_tracking()
BEGIN
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

    SELECT * FROM seniority_tracking ORDER BY highest_bps DESC, highest_bps_date;
END$$


-- ============================================================================================================================
-- Step 7 — seniority_final
-- Computes a numeric seniority rank for every employee via a self-join counting method.
-- Rank of A = 1 + (number of employees B who are strictly more senior than A).
-- Uses RPAD('~', 500) so shorter paths never incorrectly beat longer ones.
--
-- TIERS:
--   Tier 1+2+3 : seniority_path (BPS levels + dates, descending)
--   Tier 4     : dateofentryingov (earlier = more senior)
--   Tier 5     : DateOfBirth      (older   = more senior)
--   Tier 6     : ArfNo            (smaller = more senior, absolute fallback)
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_seniority_final$$
CREATE PROCEDURE sp_build_seniority_final()
BEGIN
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
            -- TIER 1+2+3: RPAD with '~' (ASCII 126) so missing segments sort LAST
            -- '~' > any real path character (digits, '-', '_', letters)
            -- Ensures employee with MORE/EARLIER career history ranks MORE senior
            RPAD(b.seniority_path, 500, '~') < RPAD(a.seniority_path, 500, '~')

            -- TIER 4: Same path, earlier govt entry wins
            OR (    b.seniority_path   =  a.seniority_path
                AND b.dateofentryingov <  a.dateofentryingov)

            -- TIER 5: Same path + same entry, older DOB wins
            OR (    b.seniority_path   =  a.seniority_path
                AND b.dateofentryingov =  a.dateofentryingov
                AND b.DateOfBirth      <  a.DateOfBirth)

            -- TIER 6: Everything equal, smaller ArfNo wins
            OR (    b.seniority_path   =  a.seniority_path
                AND b.dateofentryingov =  a.dateofentryingov
                AND b.DateOfBirth      =  a.DateOfBirth
                AND b.ArfNo            <  a.ArfNo)
        )
    GROUP BY
        a.ArfNo, a.Namee,
        a.highest_bps, a.highest_bps_date,
        a.dateofentryingov, a.DateOfBirth,
        a.seniority_path;

    SELECT * FROM seniority_final ORDER BY seniority_rank;
END$$


-- ============================================================================================================================
-- Step 8a — max_segments
-- Finds the maximum number of BPS segments any single employee has.
-- Drives Step 8b: we only generate as many depth levels as actually exist.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_max_segments$$
CREATE PROCEDURE sp_build_max_segments()
BEGIN
    DROP TABLE IF EXISTS max_segments;
    CREATE TABLE max_segments AS
    SELECT
        MAX(
            1 + LENGTH(seniority_path) - LENGTH(REPLACE(seniority_path, '_', ''))
            -- Underscores = segments - 1  →  segments = 1 + underscore count
        ) AS max_seg
    FROM seniority_final;

    SELECT * FROM max_segments;
END$$


-- ============================================================================================================================
-- Step 8b — seg_numbers
-- Integer sequence 1 .. max_seg — acts as a loop counter for Step 8c and Step 8e.
-- Extend the UNION ALL chain if any employee has more than 10 BPS career events.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_seg_numbers$$
CREATE PROCEDURE sp_build_seg_numbers()
BEGIN
    DROP TABLE IF EXISTS seg_numbers;
    CREATE TABLE seg_numbers AS
    SELECT n
    FROM (
        SELECT  1 AS n UNION ALL SELECT  2 UNION ALL SELECT  3 UNION ALL SELECT  4 UNION ALL
        SELECT  5       UNION ALL SELECT  6 UNION ALL SELECT  7 UNION ALL SELECT  8 UNION ALL
        SELECT  9       UNION ALL SELECT 10
    ) nums
    WHERE n <= (SELECT max_seg FROM max_segments);

    SELECT * FROM seg_numbers;
END$$


-- ============================================================================================================================
-- Step 8c — prefix_group_sizes2
-- For every (employee, depth N): extracts the N-segment prefix of the seniority path
-- and counts how many employees share that exact prefix.
-- cnt = 1 at depth N → this employee became unique at depth N.
--
-- PREFIX LENGTH FORMULA:
--   N segments × 11 chars + (N-1) separators = 12N - 1 characters
--   N=1→11  N=2→23  N=3→35  N=4→47  N=5→59
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_prefix_group_sizes2$$
CREATE PROCEDURE sp_build_prefix_group_sizes2()
BEGIN
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
            n.n                               AS seg_depth,
            LEFT(f.seniority_path, n.n*12-1)  AS path_prefix
        FROM seniority_final f
        CROSS JOIN seg_numbers n
        WHERE n.n <= 1 + LENGTH(f.seniority_path)
                        - LENGTH(REPLACE(f.seniority_path, '_', ''))
    ) p
    JOIN (
        -- For each unique prefix: count how many employees share it
        SELECT
            LEFT(f.seniority_path, n.n*12-1) AS path_prefix,
            COUNT(*)                          AS cnt
        FROM seniority_final f
        CROSS JOIN seg_numbers n
        WHERE n.n <= 1 + LENGTH(f.seniority_path)
                        - LENGTH(REPLACE(f.seniority_path, '_', ''))
        GROUP BY LEFT(f.seniority_path, n.n*12-1)
    ) grp ON grp.path_prefix = p.path_prefix;

    SELECT * FROM prefix_group_sizes2 ORDER BY ArfNo, seg_depth;
END$$


-- ============================================================================================================================
-- Step 8d — tiebreak_depth
-- For each employee: smallest depth at which cnt = 1 (they became unique).
-- break_depth = NULL → entire BPS path is shared with another employee;
--               Tiers 4/5/6 (entry date / DOB / ArfNo) decide.
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_tiebreak_depth$$
CREATE PROCEDURE sp_build_tiebreak_depth()
BEGIN
    DROP TABLE IF EXISTS tiebreak_depth;
    CREATE TABLE tiebreak_depth AS
    SELECT
        ArfNo,
        MIN(seg_depth) AS break_depth
    FROM   prefix_group_sizes2
    WHERE  cnt = 1
    GROUP BY ArfNo;

    SELECT * FROM tiebreak_depth ORDER BY ArfNo;
END$$


-- ============================================================================================================================
-- Step 8e — seniority_report  (FINAL OUTPUT)
-- Assembles the complete seniority ranking table with:
--   • seniority_rank          — numeric position
--   • seniority_path          — raw encoded string (audit / debug)
--   • seniority_path_readable — decoded "BPS-22: 15-Mar-2010 | BPS-17: 01-Jun-2005 | …"
--   • decision_basis          — plain-English explanation of which tiebreaker decided rank
--
-- SIX DECISION CASES:
--   Case 1: Unique BPS level              → "Unique at BPS 22"
--   Case 2: Shared BPS, unique date L1    → "Broke at L1 BPS 22 date (15-Mar-2010)"
--   Case 3: Broke at depth N > 1          → "Broke at L3 BPS 17 date (01-Jun-2005)"
--   Case 4: Identical path, unique entry  → "Broke by govt entry date (01-Jan-2000)"
--   Case 5: Identical path+entry, DOB     → "Broke by date of birth (12-Apr-1965)"
--   Case 6: Everything identical, ArfNo   → "Broke by ArfNo (smallest is most senior)"
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_build_seniority_report$$
CREATE PROCEDURE sp_build_seniority_report()
BEGIN
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

        -- Readable path: decoded generically for any number of segments
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
                ORDER BY n.n
                SEPARATOR ' | '
            )
            FROM seg_numbers n
            WHERE n.n <= 1 + LENGTH(f.seniority_path)
                            - LENGTH(REPLACE(f.seniority_path, '_', ''))
        ) AS seniority_path_readable,

        CASE

            -- CASE 1: Peak BPS is unique — no other employee has reached this BPS level
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

    -- JOIN 1: depth at which this employee first became unique
    LEFT JOIN tiebreak_depth td
        ON td.ArfNo = f.ArfNo

    -- JOIN 2: how many employees share this employee's peak BPS level (Case 1 vs Case 2)
    LEFT JOIN (
        SELECT highest_bps, COUNT(*) AS cnt
        FROM   seniority_final
        GROUP BY highest_bps
    ) grp_bps ON grp_bps.highest_bps = f.highest_bps

    -- JOIN 3: how many share the same path + govt entry date (Case 4)
    LEFT JOIN (
        SELECT seniority_path, dateofentryingov, COUNT(*) AS cnt
        FROM   seniority_final
        GROUP BY seniority_path, dateofentryingov
    ) grp_entry ON  grp_entry.seniority_path   = f.seniority_path
               AND  grp_entry.dateofentryingov  = f.dateofentryingov

    -- JOIN 4: how many share the same path + entry date + DOB (Case 5 vs Case 6)
    LEFT JOIN (
        SELECT seniority_path, dateofentryingov, DateOfBirth, COUNT(*) AS cnt
        FROM   seniority_final
        GROUP BY seniority_path, dateofentryingov, DateOfBirth
    ) grp_dob ON  grp_dob.seniority_path   = f.seniority_path
             AND  grp_dob.dateofentryingov  = f.dateofentryingov
             AND  grp_dob.DateOfBirth       = f.DateOfBirth

    ORDER BY f.seniority_rank;

    -- Return the completed report
    SELECT * FROM seniority_report ORDER BY seniority_rank;
END$$


-- ============================================================================================================================
-- MASTER PROCEDURE — runs all 13 steps in order
-- ============================================================================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS sp_run_all_seniority$$
CREATE PROCEDURE sp_run_all_seniority()
BEGIN
    CALL sp_build_combined_data();
    CALL sp_build_all_bps_events();
    CALL sp_build_bps_earliest();
    CALL sp_build_tiedgroups();
    CALL sp_build_seniority_paths();
    CALL sp_build_emp_base();
    CALL sp_build_seniority_tracking();
    CALL sp_build_seniority_final();
    CALL sp_build_max_segments();
    CALL sp_build_seg_numbers();
    CALL sp_build_prefix_group_sizes2();
    CALL sp_build_tiebreak_depth();
    CALL sp_build_seniority_report();
END$$

DELIMITER ;


-- ============================================================================================================================
-- Run every step in one call:
   CALL sp_run_all_seniority();
--
-- Or run individual steps for debugging / partial reruns:
--   CALL sp_build_combined_data();
--   CALL sp_build_all_bps_events();
--   CALL sp_build_bps_earliest();
--   CALL sp_build_tiedgroups();
--   CALL sp_build_seniority_paths();
--   CALL sp_build_emp_base();
--   CALL sp_build_seniority_tracking();
--   CALL sp_build_seniority_final();
--   CALL sp_build_max_segments();
--   CALL sp_build_seg_numbers();
--   CALL sp_build_prefix_group_sizes2();
--   CALL sp_build_tiebreak_depth();
--   CALL sp_build_seniority_report();
--
-- DROP ALL PROCEDURES (uncomment when needed):
-- DROP PROCEDURE IF EXISTS sp_build_combined_data;
-- DROP PROCEDURE IF EXISTS sp_build_all_bps_events;
-- DROP PROCEDURE IF EXISTS sp_build_bps_earliest;
-- DROP PROCEDURE IF EXISTS sp_build_tiedgroups;
-- DROP PROCEDURE IF EXISTS sp_build_seniority_paths;
-- DROP PROCEDURE IF EXISTS sp_build_emp_base;
-- DROP PROCEDURE IF EXISTS sp_build_seniority_tracking;
-- DROP PROCEDURE IF EXISTS sp_build_seniority_final;
-- DROP PROCEDURE IF EXISTS sp_build_max_segments;
-- DROP PROCEDURE IF EXISTS sp_build_seg_numbers;
-- DROP PROCEDURE IF EXISTS sp_build_prefix_group_sizes2;
-- DROP PROCEDURE IF EXISTS sp_build_tiebreak_depth;
-- DROP PROCEDURE IF EXISTS sp_build_seniority_report;
-- DROP PROCEDURE IF EXISTS sp_run_all_seniority;