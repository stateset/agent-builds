"""Forward job output without making a disconnected observer an execution failure."""

import os


class OutputRelay:
    def __init__(self):
        self.closed = {}

    def write(self, stream, data, binary=False):
        if id(stream) in self.closed:
            return False
        target = stream.buffer if binary else stream
        try:
            target.write(data)
            target.flush()
            return True
        except BrokenPipeError:
            self.closed[id(stream)] = stream
            # Python flushes standard streams at shutdown. Redirect the broken
            # descriptor so that flush cannot replace the job's exit with 120.
            # In-memory/test streams may not expose a real descriptor.
            try:
                descriptor = stream.fileno()
                if type(descriptor) is not int or descriptor < 0:
                    return False
                sink = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(sink, descriptor)
                finally:
                    if sink != descriptor:
                        os.close(sink)
            except (AttributeError, OSError, ValueError):
                pass
            return False
