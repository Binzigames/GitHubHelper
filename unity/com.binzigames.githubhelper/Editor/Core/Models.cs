using System;

namespace BinziGames.GitHubHelper
{
    [Serializable] class ErrorDto { public string error; }
    [Serializable] class StateDto { public bool git, gh, ghAuthed; public string ghUser, userName, userEmail; }
    [Serializable] class PathDto { public string path; }
    [Serializable] class FileDto { public string path, x, y, orig, m; }
    [Serializable]
    class StatusDto
    {
        public string branch, head, upstream, remote, remoteUrl, webUrl, lastSubject;
        public bool hasCommits, merging;
        public int ahead, behind;
        public FileDto[] files = new FileDto[0];
    }
    [Serializable] class BranchDto { public string name, upstream; public bool current; }
    [Serializable] class BranchesDto { public BranchDto[] local = new BranchDto[0]; public BranchDto[] remote = new BranchDto[0]; }
    [Serializable] class CommitDto { public string sha, @short, author, date, subject, refs; }
    [Serializable] class LogDto { public CommitDto[] commits = new CommitDto[0]; public string[] unpushed = new string[0]; }
    [Serializable] class CommitFileDto { public string status, path; }
    [Serializable] class ShowDto { public string sha, author, email, date, message; public CommitFileDto[] files = new CommitFileDto[0]; }
    [Serializable] class DiffDto { public string diff; }
    [Serializable] class ProgressDto { public bool active; public string op, what, phase, detail; public int pct; }
    [Serializable] class LoginDto { public string code, url, state, error; }
    [Serializable] class ToolsDto { public bool ok; public string error; }
}
