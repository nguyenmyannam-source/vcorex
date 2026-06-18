
import sys
import os

# Add the project root to the Python path to ensure that pytest can find
# all the necessary modules like 'core', 'services', 'domain', etc.,
# regardless of where the tests are run from.
# This is a standard practice for complex project layouts.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)