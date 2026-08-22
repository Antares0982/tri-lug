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
      # nixpkgs' pythonMetadataCheckPhase rejects these: the upstream aiormq /
      # aio-pika release tags carry a pyproject version that differs from the tag.
      pythonMetadataFixes = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (pyfinal: pyprev: {
            aiormq = pyprev.aiormq.overrideAttrs { dontCheckPythonMetadata = true; };
            aio-pika = pyprev.aio-pika.overrideAttrs { dontCheckPythonMetadata = true; };
          })
        ];
      };
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
                overlays = [ pythonMetadataFixes ];
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
