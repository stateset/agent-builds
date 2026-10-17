"""Build one selected command's options without duplicating argument schemas."""


class SkippedOptions:
    """Ignore nested definitions for a command that cannot be selected here."""

    def add_argument(self, *args, **kwargs):
        return None

    def add_subparsers(self, *args, **kwargs):
        return self

    def add_parser(self, *args, **kwargs):
        return self


class SelectedParsers:
    """Retain every root choice/help entry; configure only the known selection.

    Main uses this only when argv starts with the literal `run` command. The
    public parser() default still constructs the complete parser. No argparse
    internals are modified, and both paths execute the same argument schema.
    """

    def __init__(self, subparsers, selected):
        self.subparsers = subparsers
        self.selected = selected
        self.skipped = SkippedOptions()

    def add_parser(self, name, **kwargs):
        command = self.subparsers.add_parser(name, **kwargs)
        return command if name == self.selected else self.skipped
