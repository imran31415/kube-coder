# Bumps `version` in app.config.ts when App Store Connect would refuse it.
#
# Apple closes a version line once a binary from it is approved, and refuses
# later uploads to that line. EAS's autoIncrement only moves the build number,
# so without this a build of a closed version gets all the way to `eas submit`
# before failing.
#
# Only a closed version triggers a bump, so repeated TestFlight builds of an
# unreleased version keep their number.
#
# The closed line comes from the binaries attached to each App Store version,
# not the version's own label: the two can differ (a listing labelled 1.3 has
# shipped a 1.4 binary), and the public iTunes lookup only reports the label.
#
# Auth: ASC_KEY_ID / ASC_ISSUER_ID / ASC_KEY_PATH (mobile/fastlane/.env).
# Run via the mobile-build / mobile-build-ios Makefile targets.

CONFIG_PATH = File.expand_path("../app.config.ts", __dir__)
VERSION_LINE = /^(\s*version: ')([^']+)(',)$/

# App Store states where Apple has not approved the attached binary yet, so its
# version line is still open. Any other state, including ones Apple adds later,
# counts as closed: an unnecessary bump is cheap and a refused upload is not.
OPEN_STATES = %w[
  PREPARE_FOR_SUBMISSION
  WAITING_FOR_REVIEW
  IN_REVIEW
  DEVELOPER_REJECTED
  REJECTED
  METADATA_REJECTED
  INVALID_BINARY
].freeze

def next_version(local, closed)
  return local if closed.nil? || Gem::Version.new(local) > Gem::Version.new(closed)

  major, minor = Gem::Version.new(closed).segments
  "#{major}.#{minor.to_i + 1}"
end

def highest_closed_version(app)
  versions = Spaceship::ConnectAPI
    .get_app_store_versions(app_id: app.id, limit: 200, includes: "build")
    .to_models
    .reject { |v| OPEN_STATES.include?(v.app_store_state) }

  candidates = versions.flat_map do |v|
    next [v.version_string] unless v.build

    binaries = Spaceship::ConnectAPI
      .get_builds(filter: { app: app.id, version: v.build.version }, includes: "preReleaseVersion")
      .to_models
    [v.version_string] + binaries.map { |b| b.pre_release_version&.version }.compact
  end
  candidates.max_by { |c| Gem::Version.new(c) }
end

def main
  require "spaceship"

  source = File.read(CONFIG_PATH)
  matches = source.scan(VERSION_LINE)
  abort "[ios-version] expected exactly one `version: '...'` line in #{CONFIG_PATH}, found #{matches.size}" unless matches.size == 1
  local = matches.first[1]

  Spaceship::ConnectAPI.token = Spaceship::ConnectAPI::Token.create(
    key_id: ENV.fetch("ASC_KEY_ID"),
    issuer_id: ENV.fetch("ASC_ISSUER_ID"),
    filepath: ENV.fetch("ASC_KEY_PATH"),
  )
  app = Spaceship::ConnectAPI::App.find("app.kubecoder.mobile")
  abort "[ios-version] app app.kubecoder.mobile not found in App Store Connect" unless app

  closed = highest_closed_version(app)
  target = next_version(local, closed)

  if target == local
    puts "[ios-version] #{local} is open (highest closed: #{closed || 'none'}), keeping it"
  else
    File.write(CONFIG_PATH, source.sub(VERSION_LINE, "\\1#{target}\\3"))
    puts "[ios-version] #{local} is closed (highest closed: #{closed}), bumped app.config.ts to #{target}. Commit this change."
  end
end

main if __FILE__ == $PROGRAM_NAME
