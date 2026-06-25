{
  pkgs,
  antares-bot,
}:
pypkgs:
with pypkgs;
let
  aiohttp_proxy = (
    pypkgs.buildPythonPackage rec {
      pname = "aiohttp_proxy";
      version = "0.1.2";
      pyproject = true;
      src = pkgs.fetchPypi {
        inherit pname version;
        sha256 = "sha256-TaFvrOLfSGMp9+7zZxnaXCrHE251ZXTeBrQoxXk7gJA=";
      };
      build-system = [
        setuptools
      ];
      dependencies = [
        aiohttp
        yarl
      ];
    }
  );
in
(
  [
    # aria2p
    aiohttp_proxy
    feedparser
    telegraph
    # psutil
    msgpack
    mautrix  # tri_lug Matrix appservice adapter
    (pypkgs.buildPythonPackage rec {
      pname = "opencc";
      version = "ver.1.2.0";
      pyproject = true;
      src = pkgs.fetchFromGitHub {
        owner = "BYVoid";
        repo = "OpenCC";
        rev = version;
        sha256 = "sha256-T2bl4JVE04/64bLdBj5BB+2G09kDFyLnI+hx23h5q68=";
      };
      nativeBuildInputs = [
        pkgs.cmake
      ];
      preBuild = ''
        cd ..
      '';
      postInstall = ''
        cp -r python/opencc/clib/* $out/${pypkgs.python.sitePackages}/opencc/clib/
      '';
      build-system = [
        setuptools
      ];
      dependencies = [
        cmake
      ];
    })
    (moviepy.overrideAttrs {
      src = pkgs.fetchFromGitHub {
        owner = "Zulko";
        repo = "moviepy";
        rev = "0f6f6d4";
        sha256 = "sha256-y44h96xpP7g1wbplkfS+qF1vDIh6t6AINi+bIkXfjT8=";
      };
      postPatch = "";
      doInstallCheck = false;
    })
    (pypkgs.buildPythonPackage rec {
      pname = "PixivPy-Async";
      version = "1.2.14";
      pyproject = true;
      src = pkgs.fetchPypi {
        inherit pname version;
        sha256 = "sha256-Afc7ksQSGfzRnBiqSyeztyTo2nhXTw8sf2LMXgHEHCk=";
      };
      build-system = [
        setuptools
      ];
      dependencies = [
        aiohttp_proxy
        aiohttp
        aiofiles
        deprecated
      ];
    })
    (pypkgs.buildPythonPackage rec {
      pname = "pysaucenao";
      version = "1.6.2";
      pyproject = true;
      src = pkgs.fetchzip {
        url = "https://github.com/Antares0982/pysaucenao/archive/refs/tags/1.6.2.tar.gz";
        sha256 = "sha256-FGKCgTygLReYoylY53qwXf05nrs4kQRi/0xlEHJqhCU=";
      };
      build-system = [
        setuptools
      ];
      dependencies = [
        aiohttp
      ];
    })
    (pypkgs.buildPythonPackage rec {
      pname = "zhdatetime";
      version = "1.1.1";
      pyproject = true;
      src = pkgs.fetchPypi {
        inherit pname version;
        sha256 = "sha256-Se75348WrACEgKNKgPMOg9yIHxr7v6wtR444X66EdU0=";
      };
      build-system = [
        setuptools
      ];
    })
  ]
  ++ [
    (pypkgs.callPackage antares-bot { })
  ]
)
