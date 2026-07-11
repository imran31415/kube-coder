# Deterministic App Store screenshot sync for the kube-coder iOS app.
#
# Why this exists: fastlane `deliver` uploads screenshots, then runs a second
# verification pass that RE-uploads already-present images, leaving duplicates
# (our 7 + ~3 dupes = the 10-cap per device). This script bypasses that flaky
# path: per device it deletes every screenshot, then uploads exactly the 7
# source PNGs from ios-assets/<class>/ once, in 01→07 order — no retry pass.
#
# It only touches the EDITABLE (Prepare for Submission) iOS version's en-US
# screenshots. Metadata text is deliver's job (`make mobile-metadata-text`).
#
# Auth: reads ASC_KEY_ID / ASC_ISSUER_ID / ASC_KEY_PATH from the environment
# (mobile/fastlane/.env). Run via: make mobile-ios-screenshots
require "spaceship"

$stdout.sync = true                                        # stream progress live

REPO_ROOT = File.expand_path("../..", __dir__)             # scripts/ -> mobile/ -> repo
IOS_ASSETS = File.join(REPO_ROOT, "ios-assets")

# Source folder (under ios-assets/) -> App Store Connect screenshot display type.
DEVICES = {
  "iphone-6.7" => "APP_IPHONE_67",
  "iphone-6.5" => "APP_IPHONE_65",
  "ipad-12.9"  => "APP_IPAD_PRO_3GEN_129",
}.freeze

def log(msg)
  puts("[sync] #{msg}")
end

Spaceship::ConnectAPI.token = Spaceship::ConnectAPI::Token.create(
  key_id: ENV.fetch("ASC_KEY_ID"),
  issuer_id: ENV.fetch("ASC_ISSUER_ID"),
  filepath: ENV.fetch("ASC_KEY_PATH"),
)

app = Spaceship::ConnectAPI::App.find("app.kubecoder.mobile")
raise "app app.kubecoder.mobile not found" unless app

edit = app.get_edit_app_store_version(platform: "IOS")
raise "no editable iOS version — create one in App Store Connect first" unless edit
log "editable version #{edit.version_string} (#{edit.app_store_state})"

loc = edit.get_app_store_version_localizations.find { |l| l.locale == "en-US" }
raise "no en-US localization" unless loc

sets = loc.get_app_screenshot_sets

DEVICES.each do |folder, display_type|
  pngs = Dir.glob(File.join(IOS_ASSETS, folder, "*.png")).sort   # 01..07
  raise "no PNGs in ios-assets/#{folder}" if pngs.empty?

  set = sets.find { |s| s.screenshot_display_type == display_type } ||
        loc.create_app_screenshot_set(screenshot_display_type: display_type)

  existing = set.app_screenshots
  existing.each(&:delete!)
  log "#{display_type}: deleted #{existing.size}, uploading #{pngs.size}…"

  pngs.each do |png|
    set.upload_screenshot(path: png, wait_for_processing: true)
    log "  + #{File.basename(png)}"
  end

  # Enforce 01→07 order by filename. The set is briefly locked right after
  # uploads finish processing ("Can't Add/Remove Relationship when reorder"),
  # so retry with backoff, then continue — uploads already go up in 01→07 order,
  # so a skipped reorder is cosmetic at worst.
  final = set.app_screenshots.sort_by(&:file_name)
  ordered = false
  6.times do
    begin
      set.reorder_screenshots(app_screenshot_ids: final.map(&:id))
      ordered = true
      break
    rescue Spaceship::UnexpectedResponse
      sleep 10
      final = set.app_screenshots.sort_by(&:file_name)
    end
  end
  log "#{display_type}: now #{final.size} screenshots#{ordered ? ', ordered' : ' (reorder skipped — set busy)'}: #{final.map(&:file_name).join(', ')}"
end

log "done — verifying final counts"
loc.get_app_screenshot_sets.each do |s|
  next unless DEVICES.value?(s.screenshot_display_type)
  log "  #{s.screenshot_display_type}: #{s.app_screenshots.size}"
end
