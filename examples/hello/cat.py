from __future__ import print_function

import os
import sys

file = None

print("Reading message from %s" % file, file=sys.stderr)
msg = open(file).read()
print(msg)

if file != "msg.out":
    print("Saving message to msg.out")
    open("msg.out", "w").write(msg)
