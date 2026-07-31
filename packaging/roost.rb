# Homebrew formula for roost.
#
# This is the master copy; it is consumed by copying it to Formula/roost.rb in
# the tap repo (gmhoward9289-ops/homebrew-tap), which is what `brew install`
# reads. It lives here so the formula is versioned alongside the code it builds.
#
# homebrew-core is not an option yet -- it requires notability thresholds
# (stars/forks/watchers) that this project has not met.
#
# After tagging a release, refresh the checksum with:
#   curl -sL https://github.com/gmhoward9289-ops/roost/archive/refs/tags/v0.02.tar.gz | shasum -a 256
class Roost < Formula
  desc "top for Claude Code: live sessions, context use, and their subagents"
  homepage "https://github.com/gmhoward9289-ops/roost"
  url "https://github.com/gmhoward9289-ops/roost/archive/refs/tags/v0.02.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
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
