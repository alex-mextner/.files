"""modules/service-failover: the journal helper (`bin/service-failover-journal`, #197) and what the repair script makes of a unit's journal."""
import importlib.machinery
import importlib.util
import io
import itertools
import json
import random
import re
import shutil
import string
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from harness import (
    Base,
    FAILED_STATE,
    HEADER_VALUE,
    JOURNAL,
    NOTIFY_JOURNAL,
    PASSWORD_VALUE,
    REPAIR,
)

SYSTEMD_BIN = "/usr/lib/systemd/systemd"
MANAGER_PID = "2103"   # the user manager of the stubs: `systemd[2103]` in the stub journal, and the $MANAGERPID the runs get
T0 = 1794860000000000  # a journal timestamp, in microseconds


def load_journal_tool():
    loader = importlib.machinery.SourceFileLoader("service_failover_journal", str(JOURNAL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def entry(message, exe="/usr/bin/foo", comm="foo", pid="4242", when=T0, **fields):
    return {"__REALTIME_TIMESTAMP": str(when), "MESSAGE": message, "_COMM": comm, "_EXE": exe, "_PID": pid, "PRIORITY": "6", **fields}


def manager(message, **fields):
    """An entry the user manager wrote: journald's own fields for the process of the manager."""
    return entry(message, exe=SYSTEMD_BIN, comm="systemd", pid=MANAGER_PID, **fields)


def as_journal(entries):
    return "".join(json.dumps(e) + "\n" for e in entries).encode()


class LeakShapes:
    """Every shape of a credential in a log line that the ten review rounds of #197 found, as the must-not-appear table of the
    redaction: (line, the words of it that are secret). A secret of any piece of three characters or more shows in no output."""

    # Fake secrets of the shapes that slipped through a length or character gate: one to six characters, base64 padding,
    # a colon inside or at the end, a long one. Each set is (a, b, c): the templates use one to three credentials on a line.
    SECRET_SETS = (
        ("Zq7", "Wv3kP2", "Rt5y"),
        ("q9Xk2-_.", "Bn8m", "Hj6/+="),
        ("Mk4dGVzdA==", "Fp2:Cd1", "Yu3"),
        ("Lm4n" + "opqrstuvwx" + "123456", "Qa1sw2de3fr4", "Vb9nm8kl7"),
        ("Pq8x:", "Zk3:", "Mn2:"),
    )
    JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiJ0ZXN0LXVzZXIifQ", "c2lnbmF0" + "dXJlLXZhbHVl"])
    JWT_UNSIGNED = "eyJ" + "hbGciOiJub25lIn0" + ".e30."
    # every line holds credentials that must not survive; {a}, {b}, {c} are filled from SECRET_SETS, {jwt} from the JWTs
    SECRET_LINES = (
        # the two lines of #197, which the first version of the rule order let through
        "failed to refresh token: Authorization: Bearer {a}",
        "password: {a}\tAuthorization: {b}",
        # a Bearer token of any length, with or without a label in front of it
        "Bearer {a}", "bearer {a}", "BEARER\t{a}", "token: Bearer {a}", "Authorization: Bearer {a}", "authorization=Bearer {a}",
        "Proxy-Authorization:\tBearer {a}", "error: refresh token Authorization: Bearer {a} (cached)",
        # an Authorization value of any scheme, whatever separates the label from it
        "Authorization: Negotiate {a}", "Authorization: SSWS {a} and more", "Authorization: {a}", "authorization:\t{a}",
        "Authorization = {a}", "Authorization:\t\tBasic {a}", "Authorization: Custom-Scheme {a} trailing",
        '{{"Authorization": "SSWS {a}", "x": 1}}', '{{"authorization":"Bearer {a}"}}', "authorization='Custom {a}' end",
        r'{{\"Authorization\":\"Bearer {a}\"}}',
        # passwords: separators, flags, quoting, escaped JSON
        "password: {a}", "password:\t{a}", "password:  \t {a}", "password={a}", "password =\t{a}", "PASSWORD:{a}", "passwd={a}",
        "--password {a}", "--password={a}", "db_password: {a} rest", "client_secret={a}&grant=x", "api-key: {a}", "API_KEY={a}",
        'password="{a}"', "password='{a}'", 'password: "{a} {b}"', '{{"password": "{a} {b}"}}', "token: ' {a} {b} '",
        'password:\t"{a}"', 'password = "{a}"', 'password="{a}', r'{{\"password\":\"{a}\"}}', 'x {{"access_token":"{a}"}} y',
        # several credentials on one line, in every combination of the shapes above
        "password={a} token={b}", "token: password: {a}", "password: {a}\tapi_key: {b}", "secret={a};password={b}",
        'Authorization: Bearer {a}, password="{b}"', "password: {a} Authorization: Bearer {b}", "Authorization: Bearer {a}\tpassword:\t{b}",
        "token={a} Authorization: {b} secret={c}", "password: {a}\tAuthorization: {b}\ttoken: {c}", "token: Bearer {a} password: {b}",
        # a credential that ends in a colon is not the label of the next pair, and a label is not a credential
        "password={a} next=ok", "token: {a}", "password: {a} rest of the line", "secret={a}", "token: password: {a}",
        # a quoted credential after an Authorization value, with whitespace or an equals sign in front of its quote
        'Authorization: Bearer {a} password = "{b}"', "Authorization: Bearer {a} token: '{b}'", 'Authorization: Bearer {a} secret="{b}" end',
        # a label that another rule would consume must not hide its pair's value: Bearer/Basic/scheme words followed by a name,
        # an Authorization header inside a JSON string followed by another field, escaped quotes around several words
        "Invalid Bearer token: {a}", "Bearer token {a}", "Bearer secret: {a}", "token_type: bearer access_token: {a}",
        "failed to refresh bearer token: {a}", "basic password: {a}", "Basic credentials: {a}", "token: Negotiate password: {a}",
        "token=Negotiate password: {a}", "credential=Digest api_key: {a} tail", 'token: password: \\"{a} {b}\\"',
        '{{"msg":"Authorization: Bearer {a}","password":"{b},{c}"}}', '{{"message": "Authorization: {a}", "password": "\'{b}"}}',
        r'{{\"password\":\"{a} {b}\"}}', r'{{\"msg\":\"Authorization: {a}\",\"token\":\"{b} {c}\"}}',
        # hash dumps of Ruby, Perl and PHP (`=>`), escaped quotes inside a JSON value, a JSON string inside a JSON string
        '{{"password"=>"{a}"}}', "'password' => '{b}'", "[password] => {a}", "[Authorization] => Negotiate {a}", '{{"token"=>"{a} {b}"}}',
        r'{{"password":"{a}\"{b} {c}"}}', r'{{\"password\":\"{a}\\\"{b} {c}\"}}', r"{{'secret': '{a}\'{b}'}}",
        # OAuth parameters
        'Authorization: OAuth oauth_token="{a}", oauth_nonce="{b}"',
        'Authorization: OAuth oauth_consumer_key="{a}", oauth_token="{b}", oauth_signature="{c}"',
        "oauth_token={a}&oauth_token_secret={b}", 'OAuth oauth_token="{a}", oauth_signature="{b}"',
        # credentials of other shapes the module always masked
        "https://user:{a}@host/path", "X-Api-Key: {a}", "X-Auth-Token: {a} end",
        # whole JWTs, in any position
        "see {jwt} here", "Cookie: session={jwt} end", '{{"jwt":"{jwt}"}}', "Authorization: {jwt}", "token: {jwt}", "{jwt_unsigned} (unsigned)",
    )
    # shapes that name their secrets: (the secret words, the lines)
    LITERAL = (
        (("short7",), ["failed to refresh token: Authorization: Bearer short7"]),
        (("opaqueCredential",), ["error: password: x\tAuthorization: opaqueCredential"]),
        (("Alpha", "Bravo"), ["error: password=\rBravo", "password=Alpha\rBravo", "password=Alpha\x0bBravo\x0cCharlie", "token: Alpha\r\nBravo\r\n",
                              "error: password=Alpha\rBravo", 'error: password="Alpha Bravo\\"\\', "password=Alpha\\", "password=\\Alpha",
                              'password="Alpha\\\\"', 'password="Alpha Bravo\\"\\', "password='Alpha Bravo\\", 'password="Alpha Bravo\\',
                              'password: "Alpha Bravo', "token='Alpha Bravo", 'Authorization: "Alpha Bravo', r'{"a":"x","password":\"Alpha Bravo']),
        (("LeadingPart9", "TrailingPart8"), ["error: password=LeadingPart9\tTrailingPart8", "password=LeadingPart9\tTrailingPart8 next",
                                            "X-Api-Key: LeadingPart9\tTrailingPart8 end"]),
        (("Zq7", "Wv8", "Xy7", "kL9", "mN2"), ["DB_PASSWORD=Xy7;kL9,mN2 next", "secret=Zq7;password=Wv8 end", "token=Zq7, user=bob", "token: Zq7\\"]),
        (("ghp_" + "A" * 30,), ["prefix\rghp_" + "A" * 30]),
        (("f0" * 20,), ["prefix\x0b" + "f0" * 20]),
        (("dXNlcjpwYXNzd29yZA==",), ["ok\x0cBasic dXNlcjpwYXNzd29yZA=="]),
        (("abcdefghij",), ["x\rBearer abcdefghij"]),
        (("Zm9vOj8", "P3Bhc3N3b3Jk", "defghijklmnop"), [r'{"msg":"upstream rejected Basic Zm9vOj8\/P3Bhc3N3b3Jk"}', r'{"msg":"Bearer abc\/defghijklmnop"}']),
        (("UniqueSecret9",), ['X-Api-Key: "a b"UniqueSecret9 next', '{"password":"UniqueSecret9","user":"bob"}']),
        (("hunter2", "hunter3"), [
            'Config { token: Some("hunter2"), user: "bob" }', "{'password': b'hunter2'}", 'password=SecretStr("hunter2")', "secret: u'hunter2'",
            'token: Some(ApiKey("hunter2"))', 'password=SecretStr("hunter2 hunter3', "token: Some(Key('hunter2 hunter3",
            "DB_PASSWORD_2=hunter2", "secret.key=hunter2", "api_key_v2=hunter2", "Invalid API key: hunter2", "Secret Key: hunter2",
            "passphrase=hunter2", "mysql --pwd hunter2", "ssh --pass hunter2", 'private_key: "hunter2 hunter3"', "MYSQL_PWD=hunter2",
            "postgres://app:hunter2@db/x", "redis://:hunter2/cd@host", "https://user:\thunter2@host/path", "https://us\ter:hunter2@host/path",
            "mongodb://admin:hunter2/x@host:27017/db?opt=1 trailing",
            '{"msg":"upstream 401","headers":{"Authorization":["Negotiate hunter2"],"X-Api-Key":["hunter3"]}}', '{"password":["hunter2"]}',
            "{'Authorization': b'Negotiate hunter2'}", "UPDATE `users` SET `api_token`='hunter2'", "RABBITMQ_DEFAULT_PASS=hunter2",
            "{ DB_PASS: 'hunter2' }", "SMTP_PASS=hunter2", "Cookie: sessionid=hunter2; theme=dark", "Set-Cookie: PHPSESSID=hunter2; path=/",
            "sessionid=hunter2 started", "connect.sid=hunter2", '{"Cookie":"sid=hunter2; x=1"}', "pass:Bearer hunter2",
            '{"user":"bob","pass":"hunter2"}', "pass: hunter2", "auth: { user: 'x', pass: 'hunter2' }", "DB_PASS_2=hunter2", "PASS_SMTP=hunter2",
            'config["password"] = "hunter2"', "$cfg['api_key'] = 'hunter2';", "env['TOKEN']=hunter2", 'x["secret"]: "hunter2 hunter3"']),
        (("keycloakPassword9", "keyboard1234", "key_Hunter9", "key-Hunter9", "key.Hunter9", "key/Hunter9", "key+Hunter9", "key@Hunter9"),
         ["--password keycloakPassword9", "token keyboard1234", "secret key=keyboard1234", "secret keyboard1234", "--secret key_Hunter9",
          "--secret key-Hunter9", "--secret key.Hunter9", "--secret key/Hunter9", "--secret key+Hunter9", "--secret key@Hunter9 next"]),
        (("cd9", "ab'cd9", "ab\\'cd9"), ["--password='ab'\\''cd9'", "password=ab\\'cd9", "password='ab'\"'\"'cd9'", 'password="ab\\"cd9"', "password=ab\\\"cd9 next"]),
        (("open-sesame", "hexa"), ['password="<redacted-open-sesame>"', "password=<redacted-open-sesame>", "token: <redacted-hexa>"]),
        (("VisiblePart9", "LeadingSecret9"), ["https://user:VisiblePart9\t" + "A" * 2100 + "@host/path", "error: https://app:LeadingSecret9\t" + "x" * 2200 + "@db/x"]),
        (("TailMarker9", "Zq7xZq7x", "QQQQ"), [
            f"app[1]: env SESSION_SECRET={'Zq7x' * 400}TailMarker9", f"password={'Q' * 1900}TailMarker9", f"Authorization: Bearer {'Ab1' * 600}TailMarker9",
            f"Authorization: Negotiate {'YII' * 600}TailMarker9", f"Authorization: Basic {'dXNl' * 450}TailMarker9",
            f'{{"secret": "multi word value {"Zq7x " * 350}TailMarker9"}}', f"{'word ' * 300}credential: TailMarker9",
            f"client_secret_for_the_legacy_billing_integration_service_account=abcTailMarker9", "password" + "_suffix" * 6 + "=TailMarker9",
            f"https://{'u' * 300}:TailMarker9@host/path", f"X-{'Long-' * 60}Token: TailMarker9", f"{'eyJ' + 'a' * 1500 + '.' + 'b' * 300}TailMarker9",
            "start " + "word " * 600 + "password: " + "Zq7x" * 400]),
        (("MyAuthorizationSecret",), [f'password="{inner}MyAuthorizationSecret" ERROR bind 127.0.0.1:8787 failed: address already in use'
                                      for inner in ("My", "my_", "my-", "my.", "", "my ")]),
    )
    LABELS = ("password", "passwd", "token", "access_token", "secret", "client_secret", "api_key", "api-key", "apikey", "credential",
              "oauth_token", "oauth_signature", "Authorization", "Proxy-Authorization", "X-Api-Key", "PASSWORD")
    SEPARATORS = (": ", ":", "=", " = ", ":\t", "\t=\t", "  :  ", ":\t\t")
    FORMS = ("{v}", '"{v}"', "'{v}'", '"{v} and {v}2"', r'\"{v}\"', "Bearer {v}", "Negotiate {v}", "{v}:", '"{v}" tail', "{v} tail")

    @staticmethod
    def shows(secret, out):
        # a part of a secret is as good as all of it: no piece of three characters or more may stay
        return secret in out or any(piece in out for piece in re.split(r"[^A-Za-z0-9]+", secret) if len(piece) >= 3)

    @classmethod
    def used_secrets(cls, template, a, b, c):
        used = [s for s, token in ((a, "{a}"), (b, "{b}"), (c, "{c}")) if token in template]
        if "{jwt}" in template:
            used += cls.JWT.split(".")
        if "{jwt_unsigned}" in template:
            used += [cls.JWT_UNSIGNED.split(".")[0]]
        return used

    @classmethod
    def templates(cls):
        for template in cls.SECRET_LINES:
            for a, b, c in cls.SECRET_SETS:
                yield template.format(a=a, b=b, c=c, jwt=cls.JWT, jwt_unsigned=cls.JWT_UNSIGNED), cls.used_secrets(template, a, b, c)

    @classmethod
    def products(cls):
        """label x separator x value form x what follows: the pair after the first one is a password or an Authorization value."""
        n = 0
        for label, separator, form in itertools.product(cls.LABELS, cls.SEPARATORS, cls.FORMS):
            for k, joiner in enumerate((" ", "\t", ", ", "; ")):
                first, second = f"Zq{n}a", f"Wv{n}b"
                follower = f"password: {second}" if k % 2 == 0 else f"Authorization: Bearer {second}"
                yield f"{label}{separator}{form.format(v=first)}{joiner}{follower}", [first, f"{first}2", second]
                n += 1

    @classmethod
    def literal(cls):
        for secrets, lines in cls.LITERAL:
            for line in lines:
                yield line, [s for s in secrets if s in line]

    @classmethod
    def corpus(cls):
        """Every line with the secrets in it. The joined lines (any two shapes side by side, as a log line holds them) are in as well."""
        rows = [*cls.templates(), *cls.literal(), *cls.products()]
        first, second = list(cls.templates())[::7], list(cls.templates())[3::11]
        rows += [(left + joiner + right, [*ls, *rs]) for (left, ls), (right, rs) in itertools.product(first, second) for joiner in (" ", "\t")]
        return rows


class JournalTest(Base):
    """What leaves a unit's journal: `bin/service-failover-journal` builds the summary from a closed grammar (#197). The helper is
    run in this process; the repair script's runs go through the stubs."""

    tool = load_journal_tool()
    UNIT = "foo.service"

    def brief(self, entries, unit=UNIT, pid=MANAGER_PID):
        return self.tool.brief(io.BytesIO(as_journal(entries)), unit, pid)

    def notice(self, entries, unit=UNIT, pid=MANAGER_PID):
        return self.tool.notice(io.BytesIO(as_journal(entries)), unit, pid)

    def grammar(self, unit=UNIT):
        """The output G the helper promises, from the unit it was given, numbers and the closed tables: nothing else may be printed."""
        t = self.tool
        u = re.escape(unit)
        ts = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
        word = lambda members: "(?:" + "|".join(map(re.escape, sorted(members))) + ")"
        span = r"(?:[0-9]{1,9}(?:\.[0-9]{1,6})?(?:us|ms|s|min|h|d|w|month|y) ?){1,6}"
        status = r"(?:[0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])(?:/(?:" + word(t.EXIT_NAMES) + r"|n/a|RT(?:MIN|MAX)[+-][0-9]{1,2}))?"
        manager = "|".join([
            rf"{u}: (?:Main|Control) process exited, code=(?:exited|killed|dumped), status={status}",
            rf"{u}: Failed with result '{word(t.UNIT_RESULTS)}'\.",
            rf"{u}: Scheduled restart job, restart counter is at [0-9]{{1,9}}\.",
            rf"{u}: Start request repeated too quickly\.", rf"{u}: Deactivated successfully\.",
            rf"Failed to start {u}\.", rf"Dependency failed for {u}\.",
            rf"Job {u}/{word(t.JOB_TYPES)} failed with result '{word(t.JOB_RESULTS)}'\.",
            rf"{u}: Watchdog timeout \(limit {span}\)!",
            rf"{u}: {word(t.STATES)} operation timed out\. Terminating\.", rf"{u}: State '{word(t.STATES)}' timed out\. Killing\.",
            rf"{u}: A process of this unit has been killed by the OOM killer\.",
            rf"{u}: Service RestartSec={span} expired, scheduling restart\.",
        ])
        hints = word({*(x for pair in t.ERRNO_TABLE for x in pair if x), *t.EXCEPTIONS})
        counts = r"[0-9]+ (?:line|lines) of the unit's own output(?: \([0-9]+ at error priority\))?, not copied: they may hold secrets; read them with journalctl on the host"
        return re.compile("|".join([
            rf"Entries of the journal window: {ts} \.\. {ts}", rf"{ts} systemd: (?:{manager})",
            r"[0-9]+ more (?:entry|entries) written by systemd, not copied \(not a known message\)", counts,
            rf"recognised in them \(reported by the service, not verified\): {hints}(?:, {hints})*"]))

    def assert_in_grammar(self, text, unit=UNIT):
        g = self.grammar(unit)
        for line in text.splitlines():
            self.assertTrue(g.fullmatch(line), f"a line outside G: {line!r}")

    # --- what a manager entry is, and what is built of it --------------------------------------------------------------

    def test_a_manager_entry_is_rebuilt_from_a_known_message_and_never_copied(self):
        out = self.brief([
            manager("foo.service: Main process exited, code=exited, status=203/EXEC"),
            manager("foo.service: Control process exited, code=killed, status=9/KILL"),
            manager("foo.service: Failed with result 'exit-code'."),
            manager("foo.service: Scheduled restart job, restart counter is at 5."),
            manager(f"Failed to start foo.service - Foo with {PASSWORD_VALUE}."),
            manager("foo.service: Main process exited, code=exited, status=1/n/a"),
            manager("foo.service: Main process exited, code=killed, status=34/RTMIN+0"),
            manager("foo.service: Watchdog timeout (limit 1min 30s)!"),
            manager("foo.service: start-pre operation timed out. Terminating."),
            manager("foo.service: Start request repeated too quickly."),
            manager("Job foo.service/restart failed with result 'dependency'."),
        ])
        bodies = [line.split(" systemd: ", 1)[1] for line in out.splitlines() if " systemd: " in line]
        self.assertEqual(bodies, [
            "foo.service: Main process exited, code=exited, status=203/EXEC",
            "foo.service: Control process exited, code=killed, status=9/KILL",
            "foo.service: Failed with result 'exit-code'.",
            "foo.service: Scheduled restart job, restart counter is at 5.",
            "Failed to start foo.service.",     # the description is the unit file's text, not copied
            "foo.service: Main process exited, code=exited, status=1/n/a",
            "foo.service: Main process exited, code=killed, status=34/RTMIN+0",
            "foo.service: Watchdog timeout (limit 1min 30s)!",
            "foo.service: start-pre operation timed out. Terminating.",
            "foo.service: Start request repeated too quickly.",
            "Job foo.service/restart failed with result 'dependency'."])
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertRegex(out.splitlines()[0], r"^Entries of the journal window: 2026-11-\d\dT[0-9:]+Z \.\. 2026-11-\d\dT[0-9:]+Z$")
        self.assert_in_grammar(out)

    def test_a_name_that_is_not_in_a_table_is_dropped_from_the_line_not_copied(self):
        out = self.brief([manager(f"foo.service: Main process exited, code=exited, status=1/{PASSWORD_VALUE}"),
                          manager("foo.service: Main process exited, code=exited, status=300/FAILURE")])
        self.assertIn("systemd: foo.service: Main process exited, code=exited, status=1\n", out + "\n")   # the number alone
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertIn("1 more entry written by systemd, not copied", out)       # 300 is no exit status

    def test_what_is_not_exactly_a_known_message_about_the_unit_is_counted_not_copied(self):
        out = self.brief([
            manager(f"foo.service: Main process exited, code=exited, status=1/FAILURE password={PASSWORD_VALUE}"),
            manager(f"Started foo.service - token={PASSWORD_VALUE}."),
            manager(f"foo.service: Failed with result '{PASSWORD_VALUE}'."),
            manager(f"foo.service: Failed with result '{PASSWORD_VALUE}'."),
            manager(f"{PASSWORD_VALUE}.service: Main process exited, code=exited, status=1/FAILURE"),       # not the unit asked for
            manager(f"Failed to start {PASSWORD_VALUE}."),                                                  # an old systemd: the description only
            manager(f"foo.service: Watchdog timeout (limit {PASSWORD_VALUE})!"),
            manager(f"foo.service: Failed at step EXEC spawning /x/{PASSWORD_VALUE}: No such file or directory"),
            manager(f"Job foo.service/{PASSWORD_VALUE} failed with result 'done'."),
            manager("foo.service: Main process exited, code=exited, status=1/FAILURE\nsecret=" + PASSWORD_VALUE),   # a second line is dropped
        ])
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertIn("9 more entries written by systemd, not copied (not a known message)", out)
        self.assertIn("systemd: foo.service: Main process exited, code=exited, status=1/FAILURE", out)
        self.assert_in_grammar(out)

    def test_an_entry_is_the_managers_only_when_the_writing_process_is_the_manager(self):
        # A service chooses its SYSLOG_IDENTIFIER, and a process of the same user can run systemd's binary under its own name
        # and write an entry that carries _COMM and _EXE of it: only the process id (_PID) is the manager's own.
        line = "foo.service: Failed with result 'exit-code'."
        forged = [manager(line, SYSLOG_IDENTIFIER="systemd", _PID="31337"), entry(line, exe=SYSTEMD_BIN, comm="systemd"),
                  entry(line, exe="/usr/lib/systemd/systemd-evil", comm="systemd", pid=MANAGER_PID),
                  entry(line, exe=SYSTEMD_BIN, comm="(foo)", pid=MANAGER_PID),
                  {"MESSAGE": line, "_COMM": "systemd", "_PID": MANAGER_PID}, manager(line, _EXE=[SYSTEMD_BIN]), manager(line, _PID=[MANAGER_PID])]
        out = self.brief(forged)
        self.assertNotIn("systemd:", out)
        self.assertIn("7 lines of the unit's own output", out)
        unknown = self.brief([manager(line)], pid="0")                      # no manager process known: nothing is the manager's
        self.assertNotIn("systemd:", unknown)
        self.assertIn("1 line of the unit's own output", unknown)
        self.assertIn("systemd: foo.service: Failed with result 'exit-code'.", self.brief([manager(line)]))
        self.assertIn("systemd: foo.service: Failed with result 'exit-code'.", self.brief([entry(line, exe=SYSTEMD_BIN, comm="systemd", pid="1")], pid="1"))

    def test_a_message_that_is_not_text_is_read_or_set_aside_but_never_copied(self):
        raw = list(f"connect ECONNREFUSED password={PASSWORD_VALUE}".encode())
        entries = [entry(raw), entry(None), entry({"password": PASSWORD_VALUE}), entry([300, -1]), entry(12), entry(raw[:-3] + [255, 254]),
                   entry("x", PRIORITY=["3", "3"]), entry("x", _EXE={"a": 1}, **{"__REALTIME_TIMESTAMP": ["1"]})]
        out = self.brief(entries)
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertIn("8 lines of the unit's own output, not copied", out)
        self.assertIn("recognised in them (reported by the service, not verified): ECONNREFUSED", out)
        deep = b"[" * 200000 + b"]" * 200000 + b"\n"      # nested deeper than python reads: a line that is skipped, not a crash
        data = as_journal(entries) + deep + b"-- No entries --\n[1, 2]\nnot json at all\n\xff\xfe\n"
        self.assertEqual(self.tool.brief(io.BytesIO(data), self.UNIT, MANAGER_PID), out)
        self.assert_in_grammar(out)

    def test_an_entry_longer_than_the_limit_is_set_aside_without_being_read(self):
        huge = json.dumps(entry(f"password={PASSWORD_VALUE} " + "x" * (2 << 20))).encode() + b"\n"
        data = huge + as_journal([manager("foo.service: Failed with result 'exit-code'.")])
        started = time.monotonic()
        out = self.tool.brief(io.BytesIO(data), self.UNIT, MANAGER_PID)
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertIn("systemd: foo.service: Failed with result 'exit-code'.", out)
        self.assertIn("1 line of the unit's own output", out)

    # --- what the service's own output may contribute -------------------------------------------------------------------

    def test_the_service_output_is_summed_up_by_the_tables_only(self):
        out = self.brief([
            entry(f"Traceback (most recent call last): requests.exceptions.ConnectionError: HTTPSConnectionPool(host='x'): url /x?token={PASSWORD_VALUE}",
                  PRIORITY="3"),
            entry(f"Error: connect ECONNREFUSED 127.0.0.1:5432 with password: {PASSWORD_VALUE}", PRIORITY="3"),
            entry(f"java.io.IOException: Permission denied (publickey) for user bob key={PASSWORD_VALUE}"),
            entry(f"Caused by: ValueError: bad value {PASSWORD_VALUE}"),
            entry(f"\x1b[31mConnection refused\x1b[0m and \x1b[1;31mTimeoutError\x1b[m"),
            entry("the sky is blue"),
        ])
        self.assertNotIn(PASSWORD_VALUE, out)
        self.assertIn("6 lines of the unit's own output (2 at error priority), not copied", out)
        self.assertIn("recognised in them (reported by the service, not verified): Traceback (most recent call last), ConnectionError, ECONNREFUSED, "
                      "IOException, Permission denied, ValueError, Connection refused, TimeoutError", out)
        self.assert_in_grammar(out)

    def test_a_class_name_that_is_not_in_the_table_is_not_listed_whatever_it_looks_like(self):
        for text in (f"{PASSWORD_VALUE}Error: x", f"Caused by: {PASSWORD_VALUE}Exception", "VaultTokenError", "corp.PaymentsError",
                     '{"pass' + 'word":"Zq7WvError"}', "pass" + "word: Hunter2Exception"):
            with self.subTest(text):
                out = self.brief([entry(text)])
                self.assertNotIn("recognised", out)
                self.assertNotIn("Error", out)
                self.assertNotIn("Exception", out)

    def test_a_table_member_inside_a_longer_word_is_not_found(self):
        out = self.brief([entry("XECONNREFUSEDX ECONNREFUSEDS _EIO EIO_ OSErrors MyOSError"), entry("x" * 5000 + " ECONNREFUSED"), entry("ok " + "word " * 900 + "ECONNREFUSED")])
        self.assertNotIn("recognised", out)       # (the last two are over 4096 characters: the part past the last space before it is not read)

    # --- the three outputs ----------------------------------------------------------------------------------------------

    def test_the_notice_shows_the_cause_before_the_consequences_and_what_the_service_said_only_in_summary(self):
        entries = [manager("foo.service: Main process exited, code=exited, status=203/EXEC", when=T0),
                   manager("foo.service: Failed with result 'exit-code'.", when=T0 + 1),
                   manager("Failed to start foo.service - Foo.", when=T0 + 2),
                   entry(f"cannot exec: No such file or directory password={PASSWORD_VALUE}", when=T0 + 3)]
        self.assertEqual(self.notice(entries), "foo.service: Main process exited, code=exited, status=203/EXEC; "
                                               "1 line of its own output not shown (may hold secrets); recognised: No such file or directory")
        self.assertEqual(self.notice(entries[1:3]), "Failed to start foo.service.")
        self.assertEqual(self.notice([]), "(no log line)")
        self.assertEqual(self.notice([manager("foo.service: Deactivated successfully.")]), "(no log line)")   # a clean stop explains nothing
        self.assertEqual(self.notice([entry("x")]), "1 line of its own output not shown (may hold secrets)")
        self.assertLessEqual(len(self.notice([manager("foo.service: Watchdog timeout (limit 1min 30s 100ms 1us 5d 1w)!")] * 3)), 300)

    def test_the_limit_is_hit_only_when_the_manager_says_so_about_the_unit(self):
        line = "foo.service: Start request repeated too quickly."
        hit = lambda entries, unit=self.UNIT, pid=MANAGER_PID: self.tool.limit_hit(io.BytesIO(as_journal(entries)), unit, pid)
        self.assertTrue(hit([manager(line)]))
        self.assertFalse(hit([entry(line)]))                                      # the unit's own output saying so is no proof
        self.assertFalse(hit([manager(line)], pid="0"))
        self.assertFalse(hit([manager("bar.service: Start request repeated too quickly.")]))
        self.assertFalse(hit([manager("foo.service: Start request repeated too quickly. And " + PASSWORD_VALUE)]))
        self.assertFalse(hit([]))
        long_unit = "a" * 247 + ".service"                                  # 255 characters: the longest name systemd allows
        self.assertTrue(hit([manager(f"{long_unit}: Start request repeated too quickly.")], unit=long_unit))
        self.assertEqual(subprocess.run(["python3", "-I", str(JOURNAL), "limit-hit", "a" * 256, "1"], input=b"", capture_output=True).returncode, 2)
        # a manager line followed by far more output than a pipe holds: the verdict does not depend on who reads how much
        data = as_journal([manager(line), *[entry("x" * 3000) for _ in range(300)]])
        proc = subprocess.run(["bash", "-c", 'j=$(cat); python3 -I "$1" limit-hit foo.service 2103 <<<"$j"; echo $?', "_", str(JOURNAL)], input=data.decode(), capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_the_error_text_of_a_tool_becomes_one_fixed_phrase(self):
        phrases = {
            "Interactive authentication required.": "permission refused", "Access denied": "permission refused",
            "Failed to restart foo.service: Unit foo.service not found.": "unit not found", "ssh: connect to host odroidn2 port 22: Connection refused": "host unreachable",
            "ssh: Could not resolve hostname odroidn2: Name or service not known": "host unreachable", "Connection timed out": "timed out",
            "Failed to connect to user scope bus via local transport: No medium found": "no bus connection",
            "Failed to restart foo.service: Unit foo.service is masked.": "unit masked", "Job failed. See journalctl.": "reason withheld",
            "foo.service: Start request repeated too quickly.": "start limit refused",
        }
        for text, phrase in phrases.items():
            with self.subTest(text):
                self.assertEqual(self.tool.tool_error(io.BytesIO(text.encode())), phrase)
        for text in (f"Banner: password={PASSWORD_VALUE} Authorization: Bearer {HEADER_VALUE}\n", f"{PASSWORD_VALUE} is not a thing", "x" * 100000):
            out = self.tool.tool_error(io.BytesIO(text.encode()))
            self.assertIn(out, {phrase for _, phrase in self.tool.TOOL_ERRORS} | {"reason withheld"})
        self.assertEqual(self.tool.tool_error(io.BytesIO(b"")), "")
        self.assertEqual(self.tool.tool_error(io.BytesIO(b" \n\t")), "")

    # --- the property that makes the review finite ----------------------------------------------------------------------

    def test_every_output_line_of_a_journal_that_fills_every_slot_with_text_is_in_the_grammar(self):
        # Stopping rule of the review: a finding blocks when some input makes an output line leave G. This is the attacker's best
        # case for each message the manager may write (every slot filled with text of the slot's own alphabet), and for the unit's
        # own output (random lines of the shapes of the corpus): the output holds the unit, numbers and table members, no secret.
        rng = random.Random(197)
        alphabets = {"u": string.ascii_letters + string.digits + ":_.@-", "n": string.digits, "w": string.ascii_lowercase + "-",
                     "x": string.ascii_letters + string.digits + "_+/-", "s": string.digits + "msuhdwiny. ", "t": string.ascii_letters + string.digits + " .,:-_'\"="}
        tables = {"w": [*self.tool.UNIT_RESULTS, *self.tool.JOB_RESULTS, *self.tool.JOB_TYPES, *self.tool.STATES], "x": [*self.tool.EXIT_NAMES, "n/a", "RTMIN+3"],
                  "u": [self.UNIT], "n": ["0", "1", "203", "255", "256", "7"], "s": ["1min", "30s", "100ms", "1min 30s"]}
        forms = ("{u}: Main process exited, code=exited, status={n}/{x}", "{u}: Control process exited, code=killed, status={n}/{x}", "{u}: Failed with result '{w}'.", "{u}: Scheduled restart job, restart counter is at {n}.",
                 "Failed to start {u} - {t}.", "Dependency failed for {u} - {t}.", "Job {u}/{w} failed with result '{w}'.", "{u}: Watchdog timeout (limit {s})!",
                 "{u}: {w} operation timed out. Terminating.", "{u}: State '{w}' timed out. Killing.", "{u}: Service RestartSec={s} expired, scheduling restart.",
                 "{u}: Start request repeated too quickly.", "{u}: Deactivated successfully.", "{t}", "{u}: {t}")

        def fill(form):
            def one(match):
                slot = match.group(1)
                if rng.random() < 0.4 and slot in tables:
                    return rng.choice(tables[slot])
                return "".join(rng.choice(alphabets[slot]) for _ in range(rng.randint(1, 40 if slot != "n" else 9)))
            return re.sub(r"\{(.)\}", one, form)
        own = [line for line, _ in LeakShapes.corpus()]
        for batch in range(150):
            entries = []
            for _ in range(rng.randint(1, 30)):
                if rng.random() < 0.7:
                    form = fill(rng.choice(forms))
                    entries.append(manager(form, PRIORITY=rng.choice("0123456789")))
                else:
                    entries.append(entry(rng.choice(own), PRIORITY=rng.choice("0123456")))
            self.assert_in_grammar(self.brief(entries))
            self.assertLessEqual(len(self.notice(entries)), 300)

    def test_the_summary_depends_on_the_tables_and_not_on_the_text_of_the_unit(self):
        # Message replacement: swap the text of every line of the service's own output for only the table members found in it,
        # and the summary must be the same. Anything that copied a word of the text would differ.
        rows = LeakShapes.corpus()
        rng = random.Random(7)
        for start in range(0, len(rows), 60):
            lines = [line for line, _ in rows[start:start + 60]]
            for k in range(len(lines)):                       # a table member in some of the lines, in front, in the middle or behind
                if k % 3 == 0:
                    lines[k] = rng.choice(("ECONNREFUSED ", "Connection refused: ", "TimeoutError: ", "")) + lines[k]
            original = [entry(line, PRIORITY="3") for line in lines]
            reduced = [entry(" ".join(self.tool.recognised(line)), PRIORITY="3") for line in lines]
            kept = [manager("foo.service: Failed with result 'exit-code'.")]
            self.assertEqual(self.brief(original + kept), self.brief(reduced + kept))
            self.assertEqual(self.notice(original + kept), self.notice(reduced + kept))

    def assert_window_clean(self, window, entries):
        failed = manager("foo.service: Main process exited, code=exited, status=1/FAILURE")
        for text in (self.brief([*entries, failed]), self.notice([*entries, failed])):
            for line, secrets in window:
                for secret in secrets:
                    self.assertFalse(LeakShapes.shows(secret, text), f"{secret!r} of {line!r} shows in {text!r}")

    def test_no_shape_of_the_review_corpus_reaches_the_brief_or_the_notice(self):
        # The must-not-appear table: the lines of every shape the ten review rounds found, as the unit's own output and as lines that
        # pose as the manager's (wrong process, or the manager's message with the line after it); not even a piece of three characters.
        rows = LeakShapes.corpus()
        self.assertGreater(len(rows), 7000)
        kinds = (lambda line: entry(line),
                 lambda line: manager(f"foo.service: Failed with result 'exit-code'. {line}"),
                 lambda line: entry(line, exe=SYSTEMD_BIN, comm="systemd", pid="31337"))
        for make in kinds:
            for start in range(0, len(rows), 50):           # every window of the 50 entries `journalctl -n 50` gives
                window = rows[start:start + 50]
                self.assert_window_clean(window, [make(line) for line, _ in window])

    def test_hints_that_do_not_fit_are_dropped_whole_from_the_oldest_end(self):
        members = [t for pair in self.tool.ERRNO_TABLE for t in pair if t and len(t) > 25][:12]
        entries = [entry(m, when=T0 + n) for n, m in enumerate(members)]
        for text in (self.brief(entries).splitlines()[-1], self.notice(entries)):
            self.assertLessEqual(len(text), 300)
            self.assertTrue(text.endswith(members[-1]), text)         # the newest stays, whole
            self.assertNotIn(members[0], text)

    def test_a_login_banner_with_digits_does_not_change_the_manager_pid(self):
        self.set_state()
        (self.stub / "journal").write_text("2026-11-11T01:00:00+01:00 pc systemd[2103]: foo.service: Failed with result 'exit-code'.\n")
        shim = self.root / "bin" / "ssh"
        shim.write_text('#!/bin/bash\necho "Ubuntu 24.04 LTS"\nwhile [ "$#" -gt 0 ]; do case "$1" in -o) shift 2 ;; -n) shift ;; *) break ;; esac; done\nshift\nexec bash -c "$*"\n')
        shim.chmod(0o755)
        self.repair("odroidn2:user:foo.service", MANAGERPID="")
        self.assertIn("systemd: foo.service: Failed with result 'exit-code'.", (self.stub / "brief-sent").read_text())

    def test_a_failure_whose_failed_state_was_cleared_keeps_its_exit_status_in_the_summary(self):
        self.set_state("InvocationID=old\nActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\nExecMainCode=1\nExecMainStatus=78\nTransient=no\n"
                       "FragmentPath=/home/u/.config/systemd/user/foo.service\n")
        self.repair("foo.service")
        self.assertIn("Summary: exited with status 78, Result=success;", self.notifications()[0])

    def test_a_full_journal_of_the_largest_lines_is_summed_up_in_bounded_time_and_size(self):
        # `journalctl -n 50` gives 50 entries; the helper reads up to 1000 of 48 KiB (journald's LineMax) and says little of them
        heavy = [entry("token=" * 8000 + f" password={n} ECONNREFUSED", PRIORITY="3") for n in range(1000)]
        started = time.monotonic()
        out = self.brief(heavy + [manager("foo.service: Failed with result 'exit-code'.")])
        self.assertLess(time.monotonic() - started, 10)
        self.assertLess(len(out), 600)
        self.assertLessEqual(len(self.notice(heavy)), 300)
        started = time.monotonic()
        data = b"".join(json.dumps(entry("a" * 900_000)).encode() + b"\n" for _ in range(40)) + as_journal([manager("foo.service: Failed with result 'exit-code'.")])
        out = self.tool.brief(io.BytesIO(data), self.UNIT, MANAGER_PID)       # 36 MB of journal: the helper stops reading at its total limit
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotIn("systemd:", out)

    def test_the_helper_prints_nothing_until_it_has_the_whole_journal(self):
        # stopped by the probe timeout (a journal that never ends), it has printed nothing: fail closed
        proc = subprocess.run(["bash", "-c", '(echo \'{"MESSAGE": "x"}\'; sleep 2) 2>/dev/null | timeout -k 2 1 python3 -I "$1" brief foo.service 2103', "_", str(JOURNAL)],
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (124, ""))

    def test_a_usage_error_prints_nothing_to_stdout(self):
        for args in ([], ["brief"], ["brief", "foo.service"], ["brief", "foo.service", "x"], ["brief", "foo service", "1"], ["brief", "../x", "1"],
                     ["notice", "foo.service", "-1"], ["journal", "brief"], ["error", "more"], ["text"]):
            proc = subprocess.run(["python3", "-I", str(JOURNAL), *args], input=b"password=Zq7\n", capture_output=True)
            self.assertEqual((proc.returncode, proc.stdout), (2, b""), args)

    # --- the repair script on top of it ----------------------------------------------------------------------------------

    def repair(self, inst="foo.service", script=REPAIR, **extra):
        return subprocess.run(["bash", str(script), inst], capture_output=True, text=True, env={**self.env, **extra})

    def test_every_leak_shape_of_the_review_corpus_stays_out_of_the_brief_and_the_notification(self):
        corpus = LeakShapes.corpus()[-60:]
        lines = [f"2026-11-11T01:00:00+01:00 pc foo[1]: {line}\n" for line, _ in corpus[:-1]]
        lines.append("2026-11-11T01:00:02+01:00 pc systemd[2103]: foo.service: Main process exited, code=exited, status=1/FAILURE\n")
        self.set_state()
        (self.stub / "journal").write_text("".join(lines), encoding="utf-8", errors="surrogateescape")
        result = self.repair("foo.service")
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = (self.stub / "brief-sent").read_text()
        note = self.notifications()[0]
        self.assertIn("systemd: foo.service: Main process exited, code=exited, status=1/FAILURE", brief)
        self.assertIn("49 lines of the unit's own output", brief)
        for line, secrets in corpus[:-1]:
            for text in (brief, note):
                for secret in secrets:
                    self.assertFalse(LeakShapes.shows(secret, text), f"{secret} of {line!r}")

    def test_the_journal_is_read_once_as_json_of_this_boot_with_the_few_fields_the_helper_needs(self):
        self.set_state()
        self.repair("foo.service")
        self.repair("odroidn2:user:bar.service")
        fields = "-n 50 --no-pager -o json --output-fields=MESSAGE,PRIORITY,_COMM,_EXE,_PID"
        calls = self.calls()
        self.assertEqual(sum(c == f"journalctl --user -b -u foo.service {fields}" for c in calls), 1, calls)   # for the brief AND the notification
        remote = f"ssh odroidn2 journalctl --user -b -u bar.service {fields.replace(',', chr(92) + ',')}"   # the remote shell gets the words quoted
        self.assertEqual(sum(c.startswith(remote) for c in calls), 1, calls)
        self.assertTrue(any(c.startswith("ssh odroidn2 systemctl show -p MainPID --value") for c in calls), calls)   # the remote user manager's pid

    def test_the_managers_process_is_the_systemd_user_manager_or_pid_1_for_system_units(self):
        manager_lines = ("2026-11-11T01:00:00+01:00 pc systemd[2103]: foo.service: Failed with result 'exit-code'.\n"
                         "2026-11-11T01:00:01+01:00 pc systemd[1]: foo.service: Main process exited, code=exited, status=1/FAILURE\n")
        for inst, env, shown, hidden in (("foo.service", {"MANAGERPID": "2103"}, "Failed with result", "Main process exited"),
                                         ("foo.service", {"MANAGERPID": ""}, "Failed with result", "Main process exited"),      # asked of user@UID.service
                                         ("foo.service", {"MANAGERPID": "7777"}, None, "Failed with result"),
                                         ("odroidn2:system:foo.service", {}, "Main process exited", "Failed with result"),
                                         ("odroidn2:user:foo.service", {}, "Failed with result", "Main process exited")):
            with self.subTest(inst=inst, env=env):
                self.set_state()
                shutil.rmtree(self.root / "state", ignore_errors=True)
                (self.stub / "journal").write_text(manager_lines)
                self.repair(inst, **env)
                brief = (self.stub / "brief-sent").read_text()
                self.assertNotIn(hidden, brief)
                if shown:
                    self.assertIn(shown, brief)
        self.assertTrue(any(c.startswith("systemctl show -p MainPID --value user@") for c in self.calls()), self.calls())

    def test_a_line_of_the_unit_that_says_the_start_limit_hit_is_no_proof_of_it(self):
        self.set_state()
        (self.stub / "dep-refused").write_text("")           # the restart starts nothing; a failed dependency refused it
        (self.stub / "restart-logs").write_text("2026-11-11T02:00:01+01:00 pc foo[7]: foo.service: Start request repeated too quickly.\n")
        self.repair("foo.service")
        self.assertNotIn("systemctl --user reset-failed foo.service", self.calls())
        self.assertNotIn("the start limit refused the restart", (self.stub / "brief-sent").read_text())

    def test_an_unreadable_journal_is_said_so_and_does_not_stop_the_repair(self):
        self.set_state()
        (self.stub / "journal").write_text(f"2026-11-11T01:00:00+01:00 pc foo[1]: password={PASSWORD_VALUE}\n")
        (self.stub / "journal-fails").write_text("")
        result = self.repair("foo.service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(no journal summary: the journal could not be read, exit 1)", (self.stub / "brief-sent").read_text())
        self.assertTrue(self.notifications()[0].endswith("(journal could not be read: exit 1)"), self.notifications())

    def test_the_cause_is_taken_from_the_properties_read_before_first_aid(self):
        self.set_state(FAILED_STATE + "ExecMainCode=1\nExecMainStatus=78\n")
        (self.stub / "after-restart").write_text(FAILED_STATE.replace("InvocationID=old", "InvocationID=new").replace("Result=exit-code", "Result=timeout"))
        self.repair("foo.service")
        self.assertIn("Summary: exited with status 78, Result=exit-code;", self.notifications()[0])
        self.set_state(FAILED_STATE.replace("Result=exit-code", "Result=signal") + "ExecMainCode=2\nExecMainStatus=9\n")
        self.repair("bar.service")
        self.assertIn("Summary: killed by signal KILL, Result=signal;", self.notifications()[1])

    def test_what_systemctl_said_is_a_fixed_phrase_in_the_brief_and_in_the_log(self):
        self.set_state()
        (self.stub / "restart-refused").write_text(f"Failed to restart foo.service: Banner password={PASSWORD_VALUE} Authorization: Bearer {HEADER_VALUE}\n")
        self.repair("foo.service")
        brief = (self.stub / "brief-sent").read_text()
        self.assertIn("systemctl restart: refused (reason withheld)", brief)
        for secret in (PASSWORD_VALUE, HEADER_VALUE, "Banner"):
            self.assertNotIn(secret, brief)
        self.set_state()
        (self.stub / "show-fails").write_text("")
        result = self.repair("bar.service")
        self.assertIn("cannot query the unit (timed out)", result.stdout)

    def test_the_brief_tells_the_agent_the_exact_command_for_the_journal(self):
        self.set_state()
        self.repair("foo.service")
        self.assertIn("(`journalctl --user -u foo.service -n 200`)", (self.stub / "brief-sent").read_text())
        self.repair("odroidn2:system:bar.service")
        self.assertIn("(`ssh odroidn2 'journalctl -u bar.service -n 200'`)", (self.stub / "brief-sent").read_text())
        self.set_state()
        self.repair("systemd-fsck@dev-disk-by\\x2duuid-1.service")
        self.assertIn("-u systemd-fsck@dev-disk-by\\\\x2duuid-1.service -n 200`)", (self.stub / "brief-sent").read_text())

    def test_when_python3_or_the_helper_is_missing_the_brief_says_so_and_holds_no_journal_text(self):
        journal = f"2026-11-11T01:00:00+01:00 pc foo[1]: password={PASSWORD_VALUE} ECONNREFUSED\n"
        placeholder = "(no journal summary: no entries of this boot, or the helper is missing)"
        with tempfile.TemporaryDirectory() as alone:
            shim = Path(alone) / "python3"
            # only the helper's invocation (python3 -I) fails: the journalctl stub needs python3 as well
            shim.write_text(f'#!/bin/bash\n[ "$1" = -I ] && exit 127\nexec {shutil.which("python3")} "$@"\n')
            shim.chmod(0o755)
            copy = Path(alone) / "service-failover-repair"       # alone in its directory: the helper is not next to it
            shutil.copy(REPAIR, copy)
            shutil.copy(NOTIFY_JOURNAL, Path(alone) / NOTIFY_JOURNAL.name)   # (the notifier is: only the helper is missing)
            for label, script, extra in (("python3", REPAIR, {"PATH": f"{alone}:{self.env['PATH']}"}), ("helper", copy, {})):
                with self.subTest(label):
                    self.set_state()
                    (self.stub / "journal").write_text(journal)
                    (self.stub / "logged").unlink(missing_ok=True)
                    (self.stub / "brief-sent").unlink(missing_ok=True)
                    shutil.rmtree(self.root / "state", ignore_errors=True)
                    result = self.repair("foo.service", script=script, **extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    brief = (self.stub / "brief-sent").read_text()
                    self.assertIn(placeholder, brief)
                    note = self.notifications()[0]
                    self.assertTrue(note.endswith("; (no log line)"), note)
                    for text in (brief, note):
                        self.assertNotIn(PASSWORD_VALUE, text)
                        self.assertNotIn("ECONNREFUSED", text)

if __name__ == "__main__":
    unittest.main()
