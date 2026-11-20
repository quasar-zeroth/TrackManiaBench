# Original input extraction

This is the production v0.1.3 strict Trackmania 2020 extractor, adapted to use an explicit reviewed coverage policy instead of hardcoded player identities. It preserves original packed input bytes, decoded changes/events, 100 Hz held controls, source timestamps and visual samples. It rejects unsupported bitstream layouts; it never infers controls from steering visible in a video.

Build with the .NET 8 SDK and locked NuGet dependencies:

```sh
dotnet restore extractor/TrackmaniaExtractor.csproj --locked-mode
dotnet publish extractor/TrackmaniaExtractor.csproj -c Release --no-self-contained \
  -o build/extractor -p:RestoreLockedMode=true
dotnet build/extractor/TrackmaniaExtractor.dll --self-test
```

A single original ghost or native replay can be extracted with:

```sh
dotnet build/extractor/TrackmaniaExtractor.dll INPUT.Ghost.Gbx \
  --output /path/to/new/run --expected-map-uid MAP_UID \
  --expected-time-ms FINISH_MS --expected-account-id ACCOUNT_UUID
```

The destination must be new. Rejections preserve diagnostic metadata; do not treat a created directory as successful extraction. The dataset batch wrapper admits only verified outputs, pins the full published package, archives interrupted attempts, and checks completed artifacts before reuse. Supply a full executable path to `--dotnet` when using the batch wrapper.

```sh
python dataset/batch_extract_partial_v1.py /path/collection/manifest.json \
  --output /path/actions --extractor build/extractor/TrackmaniaExtractor.dll \
  --dotnet "$(command -v dotnet)" --limit 5
```

Run the first five from the same frozen collection later used for production. After the measured pilot gate passes, repeat with `--limit 250 --gate /path/pilot-gate.json --render-output /path/accepted`. Any positive count is supported; a count greater than five requires the real five-run gate. Preserve the pilot action directory and the same published extractor package when extending it.

Only explicitly reviewed, source-bound 10 ms missing prefixes are admitted through `TRACKMANIABENCH_COVERAGE_POLICY`; the default registry is empty. See [dataset policy configuration](../docs/dataset.md). A missing prefix remains absent from the action CSV and is excluded from strict/experimental training whenever the timing uncertainty overlaps it.

Pinned dependencies and license obligations are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `packages.lock.json`. Generated binaries and datasets are not part of this repository.
