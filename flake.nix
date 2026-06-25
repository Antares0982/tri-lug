{
  description = "A very basic flake";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    antares-bot = {
      url = "github:Antares0982/antares-bot";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      antares-bot,
      ...
    }:
    let
      forAllSystems =
        function:
        nixpkgs.lib.genAttrs
          [
            "x86_64-linux"
            "aarch64-linux"
            "aarch64-darwin"
          ]
          (
            system:
            function (
              import nixpkgs {
                inherit system;
              }
            )
          );
    in
    {
      devShells = forAllSystems (pkgs: rec {
        default = pkgs.callPackage ./shell.nix {
          antares-bot = antares-bot.modules.default;
        };
      });
    };
}
