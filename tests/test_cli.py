"""Argument parsing.

Regression guard for the argparse gotcha where a shared `--yes` on both the
top-level parser and the subparsers lets the subparser's default silently
overwrite a flag given before the subcommand. The symptom is a confirmation
prompt appearing despite `--yes`, which in a cron job means a run that hangs
forever instead of scraping.
"""

from __future__ import annotations

import pytest

from engine import cli


def _parse(argv):
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    args.yes = getattr(args, "yes", False)
    return args


class TestYesFlagPosition:
    def test_after_subcommand(self):
        assert _parse(["scrape", "--yes"]).yes is True

    def test_before_subcommand(self):
        assert _parse(["--yes", "scrape"]).yes is True

    def test_absent_defaults_to_false(self):
        assert _parse(["scrape"]).yes is False

    @pytest.mark.parametrize("command", ["scrape", "enrich", "run", "score",
                                         "export", "status", "init"])
    def test_every_subcommand_accepts_it_in_both_positions(self, command):
        assert _parse([command, "--yes"]).yes is True
        assert _parse(["--yes", command]).yes is True


class TestCommands:
    @pytest.mark.parametrize("command", list(cli.COMMANDS))
    def test_every_registered_command_parses(self, command):
        assert _parse([command]).command == command

    def test_command_table_matches_parser(self):
        # A subcommand the parser accepts but COMMANDS lacks would KeyError
        # at runtime instead of printing usage.
        parser = cli.build_parser()
        sub = [a for a in parser._subparsers._group_actions][0]
        assert set(sub.choices) == set(cli.COMMANDS)

    def test_unknown_command_exits(self):
        with pytest.raises(SystemExit):
            _parse(["nonsense"])


class TestExportOptions:
    def test_sheet_name_default(self):
        assert _parse(["export"]).sheet_name == "TikTok Content Intelligence"

    def test_sheet_name_override(self):
        assert _parse(["export", "--sheet-name", "Q4"]).sheet_name == "Q4"


class TestConfigErrorsAreFriendly:
    def test_missing_token_message_is_actionable(self, monkeypatch, tmp_path):
        from engine import config

        monkeypatch.delenv("APIFY_TOKEN", raising=False)
        monkeypatch.setattr(config, "ROOT", tmp_path)  # no .env to find

        with pytest.raises(config.ConfigError) as exc:
            config.apify_token()
        message = str(exc.value)
        assert "APIFY_TOKEN" in message
        assert "console.apify.com" in message

    def test_missing_accounts_file_message_is_actionable(self, tmp_path):
        from engine import config

        with pytest.raises(config.ConfigError) as exc:
            config.load_accounts(tmp_path / "nope.yml")
        assert "accounts.example.yml" in str(exc.value)
