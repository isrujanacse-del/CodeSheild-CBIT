// ============================================================================
// CODESHIELD CBIT — 24/7 SERVERLESS GOOGLE SHEETS BACKEND (Code.gs)
// Works with GitHub Pages! Zero Python server (app.py) needed!
// Target Google Sheet ID: 1CjbU4S7xRryZNoT2v7LSM8mAZ1Vi6ikS3brjDJFPXdA
//
// SETUP (Takes 30 Seconds):
// 1. Open your Google Sheet:
//    https://docs.google.com/spreadsheets/d/1CjbU4S7xRryZNoT2v7LSM8mAZ1Vi6ikS3brjDJFPXdA/edit
// 2. Click "Extensions" -> "Apps Script" in the top menu.
// 3. Delete any existing code in Code.gs, paste this entire file, and click Save (Ctrl+S).
// 4. Click "Deploy" (top right) -> "New deployment".
// 5. Click the gear icon next to "Select type" -> choose "Web app".
// 6. Set "Execute as" -> "Me".
// 7. Set "Who has access" -> "Anyone".
// 8. Click "Deploy" (authorize if prompted) and copy the "Web app URL" (ends in /exec).
// 9. Paste that Web app URL into CodeShield CBIT Admin Dashboard -> click
//    "Download index.html for GitHub Pages" and upload index.html to your GitHub repo!
// ============================================================================

const SHEET_ID = '1CjbU4S7xRryZNoT2v7LSM8mAZ1Vi6ikS3brjDJFPXdA';

function doGet(e) {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  let dbSheet = ss.getSheetByName('_Portal_State_DB');
  let st = null;
  if (dbSheet) {
    const raw = dbSheet.getRange('A2').getValue();
    if (raw) {
      try { st = JSON.parse(raw); } catch (err) { st = null; }
    }
  }
  const outJson = JSON.stringify({ ok: true, state: st, ts: Date.now() });
  if (e && e.parameter && e.parameter.callback) {
    return ContentService.createTextOutput(e.parameter.callback + '(' + outJson + ');')
      .setMimeType(ContentService.MimeType.JAVASCRIPT);
  }
  return ContentService.createTextOutput(outJson)
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);
    const incoming = JSON.parse((e && e.postData && e.postData.contents) ? e.postData.contents : '{}');
    let dbSheet = ss.getSheetByName('_Portal_State_DB');
    if (!dbSheet) {
      dbSheet = ss.insertSheet('_Portal_State_DB');
      dbSheet.getRange('A1').setValue('PORTAL_STATE_JSON');
      dbSheet.getRange('B1').setValue('LAST_UPDATED_UTC');
    }

    let existingRaw = dbSheet.getRange('A2').getValue();
    let state = { users: [], exams: [], attempts: [], subs: [], flags: [], drafts: {} };
    if (existingRaw) {
      try { state = JSON.parse(existingRaw); } catch (err) {}
    }

    // 1. Atomic Merge of Users (preserves permanent passwords & login counts across all networks)
    const userByIdent = {};
    (state.users || []).forEach(function(u) {
      userByIdent[String(u.ident || '').toLowerCase()] = u;
    });
    (incoming.users || []).forEach(function(iu) {
      const k = String(iu.ident || '').toLowerCase();
      const ex = userByIdent[k];
      if (!ex) {
        userByIdent[k] = iu;
      } else {
        if (iu.pwChanged) {
          ex.pw = iu.pw;
          ex.pwChanged = true;
          ex.mustChangePw = false;
        }
        if ((iu.loginCount || 0) >= (ex.loginCount || 0)) {
          ex.loginCount = iu.loginCount;
          ex.lastLoginAt = iu.lastLoginAt || ex.lastLoginAt;
        }
      }
    });
    state.users = Object.keys(userByIdent).map(function(k) { return userByIdent[k]; });

    // 2. Merge Exam Settings
    if (Array.isArray(incoming.exams) && incoming.exams.length > 0) {
      state.exams = incoming.exams;
    }

    // 3. Atomic Merge of Student Attempts by uid:eid
    const attMap = {};
    (state.attempts || []).forEach(function(a) { attMap[a.uid + ':' + a.eid] = a; });
    (incoming.attempts || []).forEach(function(a) {
      const k = a.uid + ':' + a.eid;
      const ex = attMap[k];
      if (!ex || a.done || (a.examLaunched && !ex.examLaunched)) {
        attMap[k] = a;
      } else if (a.lastLoginAt) {
        ex.lastLoginAt = a.lastLoginAt;
      }
    });
    state.attempts = Object.keys(attMap).map(function(k) { return attMap[k]; });

    // 4. Atomic Merge of Code Submissions by uid:qid
    const subMap = {};
    (state.subs || []).forEach(function(s) { subMap[s.uid + ':' + s.qid] = s; });
    (incoming.subs || []).forEach(function(s) { subMap[s.uid + ':' + s.qid] = s; });
    state.subs = Object.keys(subMap).map(function(k) { return subMap[k]; });

    // 5. Atomic Merge of Violation Flags by id
    const flagMap = {};
    (state.flags || []).forEach(function(f) { if (f.id) flagMap[f.id] = f; });
    (incoming.flags || []).forEach(function(f) { if (f.id) flagMap[f.id] = f; });
    state.flags = Object.keys(flagMap).map(function(k) { return flagMap[k]; });

    // 6. Merge Code Drafts
    state.drafts = Object.assign({}, state.drafts || {}, incoming.drafts || {});

    dbSheet.getRange('A2').setValue(JSON.stringify(state));
    dbSheet.getRange('B2').setValue(new Date().toISOString());

    // Update Tab 1: Student_Logins_And_Roster
    let roster = ss.getSheetByName('Student_Logins_And_Roster');
    if (!roster) roster = ss.insertSheet('Student_Logins_And_Roster');
    roster.clearContents();
    roster.appendRow(['Roll No / ID', 'Student Name', 'Batch', 'Assigned Set', 'Password Status', 'Current Password', 'Login Count', 'Last Login Time', 'Exam Status', 'Total Score / 20', 'Violation Flags']);
    state.users.filter(function(u) { return u.role === 'student'; }).forEach(function(u) {
      const att = state.attempts.find(function(a) { return a.uid === u.id; });
      const stuSubs = state.subs.filter(function(s) { return s.uid === u.id; });
      const stuFlags = state.flags.filter(function(f) { return f.uid === u.id; });
      const score = stuSubs.reduce(function(acc, s) {
        return acc + (Number(s.passed || 0) / Math.max(1, Number(s.total || 1)) * 10);
      }, 0);
      const status = att ? (att.done ? 'Submitted' : (att.examLaunched ? 'In Exam' : 'Logged In')) : (u.loginCount ? 'Logged In' : 'Not Logged In');
      roster.appendRow([
        u.ident, u.name, u.batch || 'CSE-5', 'Set #' + (u.assignedSet || 1),
        u.pwChanged ? 'Permanent Set' : 'Temp Password', u.pw || '',
        u.loginCount || 0, u.lastLoginAt || '—', status, Math.round(score * 100) / 100, stuFlags.length
      ]);
    });

    // Update Tab 2: Submissions_And_Code
    let subSheet = ss.getSheetByName('Submissions_And_Code');
    if (!subSheet) subSheet = ss.insertSheet('Submissions_And_Code');
    subSheet.clearContents();
    subSheet.appendRow(['Roll No / ID', 'Student Name', 'Batch', 'Question ID', 'Language', 'Tests Passed', 'Total Tests', 'Auto-Grade Marks', 'Submitted Source Code']);
    state.subs.forEach(function(s) {
      const stu = state.users.find(function(u) { return u.id === s.uid; }) || { ident: s.uid, name: 'Student', batch: '' };
      const marks = Math.round((Number(s.passed || 0) / Math.max(1, Number(s.total || 1))) * 10 * 100) / 100;
      subSheet.appendRow([stu.ident, stu.name, stu.batch || '', s.qid, s.lang, s.passed, s.total, marks, s.code]);
    });

    // Update Tab 3: Violation_Flags_Log
    let flagSheet = ss.getSheetByName('Violation_Flags_Log');
    if (!flagSheet) flagSheet = ss.insertSheet('Violation_Flags_Log');
    flagSheet.clearContents();
    flagSheet.appendRow(['Timestamp (UTC)', 'Roll No / ID', 'Student Name', 'Violation Type', 'Details']);
    state.flags.forEach(function(f) {
      const stu = state.users.find(function(u) { return u.id === f.uid; }) || { ident: f.uid, name: 'Student' };
      flagSheet.appendRow([f.ts || '', stu.ident, stu.name, f.kind, f.detail || '']);
    });

    return ContentService.createTextOutput(JSON.stringify({ ok: true, ts: Date.now() }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}
