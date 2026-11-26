# Build and smoke-test KasmVNC's X server from this checkout, against the pinned nixpkgs:
#   nix-build contrib/dither -A kasmvnc
#   nix-build contrib/dither -A smoke && ./result/bin/kasmvnc-smoke
# To test another build of it (a release's kasmvnc.nix, say), pass it in:
#   nix-build contrib/dither -A smoke --arg kasmvnc 'import ./kasmvnc.nix { pkgs = import ./contrib/dither/nixpkgs.nix { }; }'
{
  pkgs ? import ./nixpkgs.nix { },
  # The committed tree (what `git archive` and GitHub's tarballs hold), named as fetchFromGitHub names its output.
  src ? builtins.fetchGit { url = ../..; name = "source"; },
  version ? "dev",
  kasmvnc ? pkgs.callPackage ./package.nix { inherit src version; },
}:
{
  inherit kasmvnc;
  smoke = pkgs.writeShellApplication {
    name = "kasmvnc-smoke";
    runtimeInputs = [ kasmvnc ] ++ (with pkgs; [ python3 strace ]);
    text = ''exec python3 -I ${./smoke.py} "$@"'';
  };
}
