{ antares-bot }:
pypkgs: with pypkgs; [
  mautrix # tri_lug Matrix appservice adapter
  pytest # tri_lug test suite
  pytest-asyncio # async test support
  (pypkgs.callPackage antares-bot { })
]
