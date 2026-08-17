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


class TestBudgetWarning:
    """The actor bills ~$3.70/1,000 results against a $5/month free tier, so a
    single full sweep can eat most of the allowance. Silence here means a
    surprise bill."""

    def test_cheap_run_is_silent(self):
        assert cli._budget_warning(0.56) is None

    def test_half_the_credit_warns(self):
        msg = cli._budget_warning(2.60)
        assert msg is not None
        assert "52%" in msg

    def test_exceeding_the_credit_warns_harder(self):
        msg = cli._budget_warning(9.44)
        assert msg is not None
        assert "exceeds" in msg

    def test_full_sweep_at_real_pricing_triggers_a_warning(self):
        from engine import apify

        # 10 accounts x 100 posts = 1,000 results.
        est = 10 * 100 / 1000.0 * apify.USD_PER_1000_RESULTS
        assert est == pytest.approx(3.70)
        assert cli._budget_warning(est) is not None

    def test_recommended_first_run_is_silent(self):
        from engine import apify

        # 3 accounts x 50 posts = 150 results.
        est = 3 * 50 / 1000.0 * apify.USD_PER_1000_RESULTS
        assert est < 1.0
        assert cli._budget_warning(est) is None


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
