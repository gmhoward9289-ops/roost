# Homebrew formula for roost.
#
# This is the master copy; the homebrew-tap job in release.yml copies it to
# Formula/roost.rb in the tap repo (gmhoward9289-ops/homebrew-tap) on every
# tagged release, computing a fresh sha256 for that tag's tarball as it goes --
# so the url/sha256 below are last-release documentation, not what ships. It
# lives here so the formula is versioned alongside the code it builds.
#
# homebrew-core is not an option yet -- it requires notability thresholds
# (stars/forks/watchers) that this project has not met.
#
# The url points at the sdist tarball uploaded to the GitHub Release, not
# GitHub's auto-generated archive/refs/tags/ URL. That URL isn't a release
# asset at all, so GitHub doesn't count `brew install` downloads in the repo's
# release download_count the way it does for the .deb/.whl assets -- roost's
# Homebrew distribution was invisible to any download tracking until this
# changed.
#
# The checksum below is refreshed automatically on release: the homebrew-tap
# job in release.yml recomputes it for each tag and pushes the updated
# formula to the tap repo, so no manual step is needed. Record of what that
# job effectively runs (versionless on purpose -- see below):
#   curl -sL https://github.com/gmhoward9289-ops/roost/releases/download/v<version>/roost_top-<version>.tar.gz | shasum -a 256
#
# The version appears in this file EXACTLY ONCE, on the marked `version` line,
# and the url interpolates it. The old shape embedded it twice on the url line
# and release-please rewrote only the first occurrence -- a release once
# failed its version-consistency gate on exactly that: a url whose tag said
# the new version while its filename still said the old one. One occurrence,
# nothing to half-update, and the checker now rejects any other version
# literal in this file. The tap job seds whole url/sha256/version lines with
# computed literals, so the shipped formula never carries the interpolation.
class Roost < Formula
  include Language::Python::Shebang

  desc "top for Claude Code: live sessions, context use, and their subagents"
  homepage "https://github.com/gmhoward9289-ops/roost"
  version "0.7.0" # x-release-please-version
  url "https://github.com/gmhoward9289-ops/roost/releases/download/v#{version}/roost_top-#{version}.tar.gz"
  sha256 "87f1c68bc6d1b3c383ebec53e8e4a8d9ad197fd54b1a29abe70e2942dd625dda"
  license "MIT"

  depends_on "python@3.13"

  def install
    bin.install "roost.py" => "roost"
    # The shipped shebang is `/usr/bin/env python3`, which would resolve to
    # whatever python happens to be first on PATH -- including a virtualenv the
    # user activated for something else. Pin it to the formula's interpreter.
    rewrite_shebang detected_python_shebang(use_python_from_path: false), bin/"roost"
    man1.install "roost.1"
  end

  test do
    assert_match "roost #{version}", shell_output("#{bin}/roost --version")
    # -1 renders a frame and exits; with no Claude Code sessions present it
    # still has to produce the empty-state line rather than fail.
    assert_match(/roost|session/i, shell_output("#{bin}/roost -1 --no-color"))
  end
end
