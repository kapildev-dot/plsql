-- ============================================================
-- QUIZ ARENA : Interactive Quiz Game System  (Oracle SQL + PL/SQL)
-- Oracle 21c XE.  Run in SQL*Plus / SQL Developer BEFORE starting Flask.
--
--   sqlplus / as sysdba
--   SQL> ALTER SESSION SET CONTAINER = XEPDB1;   -- only if you use the PDB
--   SQL> @app.sql
--
-- Objects created:
--   TABLE      quiz_result
--   FUNCTIONS  get_result_status, get_grade, get_player_rank
--   PROCEDURES insert_quiz_result, get_leaderboard (SYS_REFCURSOR)
--   TRIGGER    trg_quiz_completed
--
-- Script is RE-RUNNABLE: it drops the old quiz_result first
-- (old 3-column table + its data will be removed).
-- ============================================================
SET SERVEROUTPUT ON;

-- 0. CLEAN RE-RUN ----------------------------------------------
BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE quiz_result PURGE';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE != -942 THEN RAISE; END IF;      -- ORA-00942: table does not exist
END;
/
BEGIN
    EXECUTE IMMEDIATE 'DROP SEQUENCE quiz_seq';
EXCEPTION WHEN OTHERS THEN
    IF SQLCODE != -2289 THEN RAISE; END IF;     -- ORA-02289: sequence does not exist
END;
/

-- 1. TABLE -----------------------------------------------------
CREATE TABLE quiz_result (
    student_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_name   VARCHAR2(50) NOT NULL,
    category       VARCHAR2(20) NOT NULL,
    score          NUMBER(3)    NOT NULL,            -- correct answers
    total_q        NUMBER(3)    NOT NULL,            -- questions in the round
    points         NUMBER(6)    DEFAULT 0 NOT NULL,
    time_spent     NUMBER(6,1)  DEFAULT 0 NOT NULL,  -- seconds
    result_status  VARCHAR2(4)  NOT NULL,
    grade          VARCHAR2(2)  NOT NULL,
    played_at      TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT chk_quiz_score  CHECK (score >= 0 AND score <= total_q),
    CONSTRAINT chk_quiz_status CHECK (result_status IN ('PASS', 'FAIL'))
);

CREATE INDEX idx_quiz_rank ON quiz_result (points DESC, time_spent ASC);

-- 2. FUNCTION : PASS / FAIL  (60% or more = PASS) ---------------
CREATE OR REPLACE FUNCTION get_result_status (
    p_score IN NUMBER,
    p_total IN NUMBER
) RETURN VARCHAR2
IS
BEGIN
    IF p_total IS NULL OR p_total = 0 THEN
        RETURN 'FAIL';
    END IF;

    IF (p_score * 100 / p_total) >= 60 THEN
        RETURN 'PASS';
    END IF;
    RETURN 'FAIL';
END get_result_status;
/

-- 3. FUNCTION : GRADE -------------------------------------------
CREATE OR REPLACE FUNCTION get_grade (
    p_score IN NUMBER,
    p_total IN NUMBER
) RETURN VARCHAR2
IS
    v_pct NUMBER;
BEGIN
    IF p_total IS NULL OR p_total = 0 THEN
        RETURN 'F';
    END IF;

    v_pct := p_score * 100 / p_total;
    IF    v_pct >= 90 THEN RETURN 'A+';
    ELSIF v_pct >= 80 THEN RETURN 'A';
    ELSIF v_pct >= 70 THEN RETURN 'B';
    ELSIF v_pct >= 60 THEN RETURN 'C';
    ELSE                   RETURN 'F';
    END IF;
END get_grade;
/

-- 4. PROCEDURE : insert result (calls both functions) ------------
CREATE OR REPLACE PROCEDURE insert_quiz_result (
    p_name     IN  VARCHAR2,
    p_category IN  VARCHAR2,
    p_score    IN  NUMBER,
    p_total    IN  NUMBER,
    p_points   IN  NUMBER,
    p_time     IN  NUMBER,
    p_status   OUT VARCHAR2,
    p_grade    OUT VARCHAR2,
    p_id       OUT NUMBER
)
IS
BEGIN
    p_status := get_result_status(p_score, p_total);
    p_grade  := get_grade(p_score, p_total);

    INSERT INTO quiz_result
        (student_name, category, score, total_q, points, time_spent, result_status, grade)
    VALUES
        (p_name, p_category, p_score, p_total, p_points, p_time, p_status, p_grade)
    RETURNING student_id INTO p_id;
    -- COMMIT is done by the caller (Flask) so the procedure stays reusable
END insert_quiz_result;
/

-- 5. TRIGGER : fires after every insert --------------------------
CREATE OR REPLACE TRIGGER trg_quiz_completed
AFTER INSERT ON quiz_result
FOR EACH ROW
BEGIN
    DBMS_OUTPUT.PUT_LINE('Quiz Completed Successfully');
END trg_quiz_completed;
/

-- 6. FUNCTION : player rank (points DESC, time ASC, id ASC) -------
CREATE OR REPLACE FUNCTION get_player_rank (
    p_id IN NUMBER
) RETURN NUMBER
IS
    v_points quiz_result.points%TYPE;
    v_time   quiz_result.time_spent%TYPE;
    v_rank   NUMBER;
BEGIN
    SELECT points, time_spent
      INTO v_points, v_time
      FROM quiz_result
     WHERE student_id = p_id;

    SELECT COUNT(*) + 1
      INTO v_rank
      FROM quiz_result
     WHERE points > v_points
        OR (points = v_points AND time_spent < v_time)
        OR (points = v_points AND time_spent = v_time AND student_id < p_id);

    RETURN v_rank;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        RETURN NULL;
END get_player_rank;
/

-- 7. PROCEDURE : leaderboard through SYS_REFCURSOR ----------------
CREATE OR REPLACE PROCEDURE get_leaderboard (
    p_limit  IN  NUMBER,
    p_cursor OUT SYS_REFCURSOR
)
IS
BEGIN
    OPEN p_cursor FOR
        SELECT rnk, student_id, student_name, category,
               points, score, total_q, result_status
          FROM (SELECT q.*,
                       ROW_NUMBER() OVER (ORDER BY points DESC,
                                                   time_spent ASC,
                                                   student_id ASC) AS rnk
                  FROM quiz_result q)
         WHERE rnk <= p_limit
         ORDER BY rnk;
END get_leaderboard;
/

SHOW ERRORS;

-- 8. TEST (optional) ---------------------------------------------
DECLARE
    v_status VARCHAR2(10);
    v_grade  VARCHAR2(10);
    v_id     NUMBER;
BEGIN
    insert_quiz_result('Test Student', 'Mixed', 8, 10, 1450, 62.5,
                       v_status, v_grade, v_id);
    DBMS_OUTPUT.PUT_LINE('Status: ' || v_status || ' | Grade: ' || v_grade ||
                         ' | Rank: ' || get_player_rank(v_id));
    COMMIT;
END;
/

SELECT student_id, student_name, category, score, total_q, points,
       result_status, grade
  FROM quiz_result;