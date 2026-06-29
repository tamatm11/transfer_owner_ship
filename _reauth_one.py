import argparse

from transfer_ownership import reauthorize_credentials


parser = argparse.ArgumentParser()
parser.add_argument("token_path")
parser.add_argument("email")
args = parser.parse_args()

reauthorize_credentials(
    "credentials.json",
    args.token_path,
    expected_email=args.email,
)
