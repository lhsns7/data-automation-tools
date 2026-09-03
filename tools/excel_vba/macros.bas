Attribute VB_Name = "ExcelTools"
' ============================================================
'  엑셀 자동화 매크로 (VBA) — 2026-08
'  실무 최빈 3종: ①다중 시트 통합 ②데이터 정리 ③그룹 집계 요약
'
'  설계 원칙
'   - 각 기능 = Do*() Function(결과 반환, 대화상자 없음) + 한글 Sub(버튼 연결, MsgBox 안내).
'     → Function은 headless 검증(LibreOffice)에서 그대로 호출 가능.
'   - Scripting.Dictionary·Collection(키없는 Add) 회피 → 배열+선형탐색(DoSummarize). LibreOffice 8/8 실증.
'   - Excel/LibreOffice 공통 문법만 사용. 대상 = ActiveWorkbook.
'   - 방어: 빈 시트·데이터 없음·시트 1개 등 경계 처리.
' ============================================================
Option Explicit
Public Const APP_NAME As String = "엑셀 자동화 도구"

' 시트의 마지막 데이터 행(1열 기준). 빈 시트면 0.
Private Function LastRow(ws As Worksheet) As Long
    LastRow = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    If LastRow = 1 And Trim(CStr(ws.Cells(1, 1).Value)) = "" Then LastRow = 0
End Function

' 시트의 마지막 데이터 열(1행 기준). 빈 시트면 0.
Private Function LastCol(ws As Worksheet) As Long
    LastCol = ws.Cells(1, ws.Columns.Count).End(xlToLeft).Column
    If LastCol = 1 And Trim(CStr(ws.Cells(1, 1).Value)) = "" Then LastCol = 0
End Function

' 한 행을 탭 구분 문자열로(중복 판정 키).
Private Function RowKey(ws As Worksheet, r As Long, nCol As Long) As String
    Dim c As Long, s As String
    For c = 1 To nCol
        s = s & Trim(CStr(ws.Cells(r, c).Value)) & Chr(9)
    Next c
    RowKey = s
End Function

' Collection에 키가 있는지(Dictionary 대체).
Private Function HasKey(col As Collection, k As String) As Boolean
    On Error Resume Next
    Dim v As Variant
    v = col(k)
    HasKey = (Err.Number = 0)
    On Error GoTo 0
End Function

' ============================================================
'  1) 다중 시트 통합 — 모든 시트를 "통합" 시트 하나로. 헤더(1행)는 첫 시트만.
'     반환 = 통합된 데이터 행수(헤더 제외).
' ============================================================
Public Function DoMergeSheets() As Long
    Dim ws As Worksheet, dst As Worksheet
    Dim lr As Long, lc As Long, dstRow As Long, headerCols As Long
    Dim first As Boolean

    Application.ScreenUpdating = False
    Application.DisplayAlerts = False
    On Error Resume Next
    ActiveWorkbook.Worksheets("통합").Delete
    On Error GoTo 0
    Application.DisplayAlerts = True

    Set dst = ActiveWorkbook.Worksheets.Add(After:=ActiveWorkbook.Worksheets(ActiveWorkbook.Worksheets.Count))
    dst.Name = "통합"
    dstRow = 1
    first = True
    headerCols = 0

    For Each ws In ActiveWorkbook.Worksheets
        If ws.Name <> "통합" Then
            lr = LastRow(ws)
            lc = LastCol(ws)
            If lr >= 1 And lc >= 1 Then
                If first Then
                    ws.Range(ws.Cells(1, 1), ws.Cells(lr, lc)).Copy dst.Cells(dstRow, 1)
                    dstRow = dstRow + lr
                    headerCols = lc
                    first = False
                Else
                    If lr >= 2 Then
                        ws.Range(ws.Cells(2, 1), ws.Cells(lr, lc)).Copy dst.Cells(dstRow, 1)
                        dstRow = dstRow + (lr - 1)
                    End If
                End If
            End If
        End If
    Next ws

    Application.CutCopyMode = False
    Application.ScreenUpdating = True
    DoMergeSheets = dstRow - 1 - IIf(headerCols > 0, 1, 0)   ' 헤더 1행 제외
End Function

Public Sub 시트통합()
    Dim n As Long
    n = DoMergeSheets()
    MsgBox "여러 시트를 '통합' 시트로 합쳤습니다." & vbCrLf & _
           "데이터 " & n & "행 (헤더 제외).", vbInformation, APP_NAME
End Sub

' ============================================================
'  2) 데이터 정리 — 활성 시트: 완전 빈 행 삭제 + 중복 행 제거 + 셀 앞뒤 공백 제거.
'     반환 = 삭제된 행수(빈 행 + 중복). 헤더(1행)는 보존.
' ============================================================
Public Function DoCleanData() As Long
    Dim ws As Worksheet
    Dim lr As Long, lc As Long, r As Long, c As Long
    Dim removed As Long
    Dim seen As Collection
    Dim k As String, isBlank As Boolean

    Set ws = ActiveWorkbook.ActiveSheet
    lr = LastRow(ws)
    lc = LastCol(ws)
    If lr < 2 Or lc < 1 Then
        DoCleanData = 0
        Exit Function
    End If

    Application.ScreenUpdating = False
    ' 공백 트림
    For r = 1 To lr
        For c = 1 To lc
            If VarType(ws.Cells(r, c).Value) = vbString Then
                ws.Cells(r, c).Value = Trim(CStr(ws.Cells(r, c).Value))
            End If
        Next c
    Next r

    Set seen = New Collection
    removed = 0
    ' 아래에서 위로 삭제(인덱스 밀림 방지). 헤더(1행) 제외.
    For r = lr To 2 Step -1
        ' 완전 빈 행?
        isBlank = True
        For c = 1 To lc
            If Trim(CStr(ws.Cells(r, c).Value)) <> "" Then isBlank = False: Exit For
        Next c
        If isBlank Then
            ws.Rows(r).Delete
            removed = removed + 1
        Else
            k = RowKey(ws, r, lc)
            If HasKey(seen, k) Then
                ws.Rows(r).Delete
                removed = removed + 1
            Else
                seen.Add r, k
            End If
        End If
    Next r

    Application.ScreenUpdating = True
    DoCleanData = removed
End Function

Public Sub 데이터정리()
    Dim n As Long
    n = DoCleanData()
    MsgBox "빈 행과 중복 행을 정리했습니다." & vbCrLf & _
           "삭제 " & n & "행 (앞뒤 공백도 제거).", vbInformation, APP_NAME
End Sub

' ============================================================
'  3) 그룹 집계 요약 — 활성 시트(헤더+데이터)에서 groupCol 기준 valueCol 합계 → "요약" 시트.
'     반환 = 그룹 수. groupCol/valueCol = 1-기준 열 번호.
' ============================================================
Public Function DoSummarize(ByVal groupCol As Long, ByVal valueCol As Long) As Long
    Dim ws As Worksheet, sm As Worksheet
    Dim lr As Long, r As Long, i As Long, found As Long, nG As Long
    Dim g As String, v As Double
    Dim gnames() As String, gsum() As Double

    Set ws = ActiveWorkbook.ActiveSheet
    lr = LastRow(ws)
    If lr < 2 Then
        DoSummarize = 0
        Exit Function
    End If

    ' ★Collection/HasKey 미사용(LibreOffice VBASupport에서 키 접근 오작동 — 없는 키가 오류 안 나
    '   HasKey가 항상 True → Add 스킵). 그룹명 배열 + 선형 탐색으로 대체(Excel·LibreOffice 공통).
    ReDim gnames(1 To lr)
    ReDim gsum(1 To lr)
    nG = 0
    For r = 2 To lr
        g = Trim(CStr(ws.Cells(r, groupCol).Value))
        If g = "" Then g = "(빈값)"
        v = 0
        If IsNumeric(ws.Cells(r, valueCol).Value) Then v = CDbl(ws.Cells(r, valueCol).Value)
        found = 0
        For i = 1 To nG
            If gnames(i) = g Then found = i: Exit For
        Next i
        If found = 0 Then
            nG = nG + 1
            gnames(nG) = g
            gsum(nG) = v
        Else
            gsum(found) = gsum(found) + v
        End If
    Next r

    Application.ScreenUpdating = False
    Application.DisplayAlerts = False
    On Error Resume Next
    ActiveWorkbook.Worksheets("요약").Delete
    On Error GoTo 0
    Application.DisplayAlerts = True
    Set sm = ActiveWorkbook.Worksheets.Add(After:=ActiveWorkbook.Worksheets(ActiveWorkbook.Worksheets.Count))
    sm.Name = "요약"
    sm.Cells(1, 1).Value = ws.Cells(1, groupCol).Value
    sm.Cells(1, 2).Value = CStr(ws.Cells(1, valueCol).Value) & " 합계"
    For i = 1 To nG
        sm.Cells(i + 1, 1).Value = gnames(i)
        sm.Cells(i + 1, 2).Value = gsum(i)
    Next i
    Application.ScreenUpdating = True
    DoSummarize = nG
End Function

' 기본 열(1=그룹, 2=값) 고정 버전 — 대부분 A열 그룹·B열 값. 인자 없는 호출용.
Public Function DoSummarizeAB() As Long
    DoSummarizeAB = DoSummarize(1, 2)
End Function

Public Sub 그룹집계()
    Dim gc As String, vc As String
    gc = InputBox("그룹으로 묶을 열 번호 (예: 1)", APP_NAME, "1")
    If gc = "" Then Exit Sub
    vc = InputBox("합계 낼 값 열 번호 (예: 2)", APP_NAME, "2")
    If vc = "" Then Exit Sub
    Dim n As Long
    n = DoSummarize(CLng(gc), CLng(vc))
    MsgBox "'요약' 시트에 그룹 " & n & "개 집계를 만들었습니다.", vbInformation, APP_NAME
End Sub
