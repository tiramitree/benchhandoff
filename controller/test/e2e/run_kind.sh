#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly KIND_VERSION="v0.32.0"
readonly KIND_SHA256="50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54"
readonly KIND_NODE_IMAGE="kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5"
readonly KUBECTL_VERSION="v1.36.1"
readonly KUBECTL_SHA256="629d3f410e09bf49b64ae7079f7f0bda1191efed311f7d37fdbab0ad5b0ec2b7"
readonly PYTHON_BASE_IMAGE="docker.io/library/python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
readonly REGISTRY_IMAGE="docker.io/library/registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
readonly SYSTEM_NAMESPACE="benchhandoff-system"
readonly TEST_NAMESPACE="benchhandoff-e2e"
readonly LEASE_NAME="agentrun-controller.benchhandoff.dev"
readonly LABEL_RUN_UID="control.benchhandoff.dev/run-uid"
readonly LABEL_ACTION="control.benchhandoff.dev/action"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SUITE_SHA256=""
WRONG_SUITE_SHA256=""
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd -P)"
CONTROLLER_DIR="$REPOSITORY_ROOT/controller"
MANIFEST_DIR="$SCRIPT_DIR/manifests"
FIXTURE_DIR="$SCRIPT_DIR/fixture"

TEMP_PARENT="${RUNNER_TEMP:-/tmp}"
mkdir -p -- "$TEMP_PARENT"
TEMP_PARENT="$(cd -- "$TEMP_PARENT" && pwd -P)"
SCRATCH_ROOT="$(mktemp -d "$TEMP_PARENT/benchhandoff-agentrun-e2e.XXXXXX")"
TOOLS_DIR="$SCRATCH_ROOT/bin"
DATA_ROOT="$SCRATCH_ROOT/data"
RENDERED_DIR="$SCRATCH_ROOT/rendered"
mkdir -p -- "$TOOLS_DIR" "$DATA_ROOT" "$RENDERED_DIR"

run_identity="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$"
cluster_suffix="$(printf '%s' "$run_identity" | sha256sum | cut -c1-10)"
readonly CLUSTER_NAME="benchhandoff-e2e-$cluster_suffix"
readonly MANAGER_IMAGE="benchhandoff-agentrun-controller:e2e-$cluster_suffix"
readonly REGISTRY_NAME="benchhandoff-registry-$cluster_suffix"
EVIDENCE_DIR_REQUESTED="${E2E_EVIDENCE_DIR:-$SCRATCH_ROOT/evidence}"
EVIDENCE_DIR=""
EVIDENCE_DIR_OWNED=""
EVIDENCE_COMPLETE=""
CONTROLLER_BUILD_DIR="$CONTROLLER_DIR/.build"
CONTROLLER_BUILD_DIR_OWNED=""
RUNNER_IMAGE=""
RUNNER_DIGEST=""
REGISTRY_CREATED=""
CLUSTER_CREATED=""
CLUSTER_CREATE_ATTEMPTED=""
KIND_NODE_NAME=""
KIND_NODE_UID=""
NODE_CORDONED=""
HAPPY_WATCH_PID=""
SOURCE_REVISION=""
TAKEOVER_HOLDER_BEFORE=""
TAKEOVER_HOLDER_AFTER=""
TAKEOVER_MANAGER_BEFORE=""
TAKEOVER_MANAGER_AFTER=""
TAKEOVER_MANAGER_BEFORE_UID=""
TAKEOVER_MANAGER_AFTER_UID=""
TAKEOVER_PASSIVE_BEFORE=""
TAKEOVER_PASSIVE_BEFORE_UID=""
TAKEOVER_TRANSITIONS_BEFORE=""
TAKEOVER_TRANSITIONS_AFTER=""
TAKEOVER_LEASE_RESOURCE_VERSION_BEFORE=""
TAKEOVER_LEASE_RESOURCE_VERSION_AFTER=""
TAKEOVER_JOB_AFTER_NAME=""
TAKEOVER_JOB_AFTER_UID=""
TAKEOVER_POD_AFTER_NAME=""
TAKEOVER_POD_AFTER_UID=""
TAKEOVER_JOB_COUNT_BEFORE=""
TAKEOVER_JOB_COUNT_AFTER=""
TAKEOVER_POD_COUNT_BEFORE=""
TAKEOVER_POD_COUNT_AFTER=""
readonly HOST_UID="$(id -u)"
readonly HOST_GID="$(id -g)"
readonly KIND="$TOOLS_DIR/kind"
readonly KUBECTL="$TOOLS_DIR/kubectl"

fail() {
  echo "ERROR: $*" >&2
  return 1
}

bind_clean_source_revision() {
  local status
  SOURCE_REVISION="$(git -C "$REPOSITORY_ROOT" rev-parse --verify 'HEAD^{commit}')"
  [[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] ||
    fail "source revision was not one canonical commit"
  status="$(git -C "$REPOSITORY_ROOT" status \
    --porcelain=v1 \
    --untracked-files=all)"
  [[ -z "$status" ]] ||
    fail "source tree was not clean; refusing commit-bound evidence"
  readonly SOURCE_REVISION
}

verify_bound_source_revision() {
  local current_revision status
  current_revision="$(git -C "$REPOSITORY_ROOT" rev-parse --verify 'HEAD^{commit}')"
  status="$(git -C "$REPOSITORY_ROOT" status \
    --porcelain=v1 \
    --untracked-files=all)"
  [[ "$current_revision" == "$SOURCE_REVISION" && -z "$status" ]] ||
    fail "source tree changed after the E2E revision was bound"
}

prepare_evidence_directory() {
  local requested parent leaf canonical_parent canonical_target
  requested="${EVIDENCE_DIR_REQUESTED%/}"
  parent="$(dirname -- "$requested")"
  leaf="$(basename -- "$requested")"
  [[ "$leaf" == "evidence" ||
    "$leaf" =~ ^benchhandoff-agentrun-evidence-[0-9]+-[0-9]+$ ]] ||
    fail "E2E evidence directory name was outside the bounded pattern"
  [[ -d "$parent" && ! -L "$parent" ]] ||
    fail "E2E evidence parent was not one existing ordinary directory"
  canonical_parent="$(cd -- "$parent" && pwd -P)"
  canonical_target="$canonical_parent/$leaf"
  case "$canonical_target" in
    "$TEMP_PARENT"/*)
      ;;
    *)
      fail "E2E evidence directory was outside the temporary root"
      ;;
  esac
  [[ ! -e "$canonical_target" && ! -L "$canonical_target" ]] ||
    fail "refusing to overwrite an existing E2E evidence directory"
  mkdir -- "$canonical_target"
  EVIDENCE_DIR="$canonical_target"
  EVIDENCE_DIR_OWNED="yes"
  chmod 0700 -- "$canonical_target"
  EVIDENCE_DIR="$(cd -- "$canonical_target" && pwd -P)"
  [[ "$EVIDENCE_DIR" == "$canonical_target" ]] ||
    fail "E2E evidence directory changed during creation"
  readonly EVIDENCE_DIR
}

restore_node_schedulability() {
  local current_context current_uid unschedulable
  [[ "$NODE_CORDONED" == "yes" ]] || return 0
  [[ "$CLUSTER_CREATED" == "yes" &&
    "$KIND_NODE_NAME" == "$CLUSTER_NAME-control-plane" &&
    "$KIND_NODE_UID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    return 1
  current_context="$("$KUBECTL" config current-context 2>/dev/null)" ||
    return 1
  [[ "$current_context" == "kind-$CLUSTER_NAME" ]] || return 1
  current_uid="$("$KUBECTL" get node "$KIND_NODE_NAME" \
    --request-timeout=10s \
    -o jsonpath='{.metadata.uid}' 2>/dev/null)" ||
    return 1
  [[ "$current_uid" == "$KIND_NODE_UID" ]] || return 1
  "$KUBECTL" uncordon "$KIND_NODE_NAME" \
    --request-timeout=10s >/dev/null 2>&1 ||
    return 1
  unschedulable="$("$KUBECTL" get node "$KIND_NODE_NAME" \
    --request-timeout=10s \
    -o jsonpath='{.spec.unschedulable}' 2>/dev/null)" ||
    return 1
  [[ -z "$unschedulable" || "$unschedulable" == "false" ]] || return 1
  NODE_CORDONED=""
}

bounded_diagnostics() {
  if [[ -x "$KUBECTL" ]]; then
    "$KUBECTL" get agentruns.control.benchhandoff.dev -A \
      --request-timeout=10s \
      -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,REASON:.status.conditions[-1].reason' \
      2>/dev/null || true
    "$KUBECTL" get jobs,pods -n "$TEST_NAMESPACE" \
      --request-timeout=10s \
      -o custom-columns='KIND:.kind,NAME:.metadata.name,PHASE:.status.phase,SUCCEEDED:.status.succeeded,FAILED:.status.failed' \
      2>/dev/null || true
  fi
}

restore_scratch_ownership() {
  case "$SCRATCH_ROOT" in
    "$TEMP_PARENT"/benchhandoff-agentrun-e2e.*)
      ;;
    *)
      echo "Refusing cleanup for an unexpected scratch identity" >&2
      return 1
      ;;
  esac
  [[ "$DATA_ROOT" == "$SCRATCH_ROOT/data" ]] || {
    echo "Refusing cleanup for an unexpected data identity" >&2
    return 1
  }
  [[ "$HOST_UID" =~ ^[0-9]+$ && "$HOST_GID" =~ ^[0-9]+$ ]] || {
    echo "Refusing cleanup with an invalid host identity" >&2
    return 1
  }
  if [[ ! -d "$DATA_ROOT" ]]; then
    return 0
  fi
  if [[ "$CLUSTER_CREATED" != "yes" ]]; then
    if [[ "$CLUSTER_CREATE_ATTEMPTED" == "yes" ]]; then
      echo "Cluster creation was incomplete; scratch ownership is uncertain" >&2
      return 1
    fi
    chmod -R u+rwX -- "$DATA_ROOT" >/dev/null 2>&1 || {
      echo "Host cleanup permission restoration failed" >&2
      return 1
    }
    return
  fi

  nodes_output="$("$KIND" get nodes --name "$CLUSTER_NAME" 2>/dev/null)"
  nodes_status="$?"
  [[ "$nodes_status" == "0" ]] || {
    echo "Could not verify the cleanup node" >&2
    return 1
  }
  cleanup_nodes=()
  while IFS= read -r cleanup_node; do
    if [[ -n "$cleanup_node" ]]; then
      cleanup_nodes+=("$cleanup_node")
    fi
  done <<<"$nodes_output"
  [[ "${#cleanup_nodes[@]}" == "1" ]] || {
    echo "Cleanup node cardinality was not one" >&2
    return 1
  }
  cleanup_node="${cleanup_nodes[0]}"
  [[ "$cleanup_node" == "$CLUSTER_NAME-control-plane" ]] || {
    echo "Cleanup node name did not match the bounded cluster" >&2
    return 1
  }
  node_cluster="$(docker inspect --format \
    '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' \
    "$cleanup_node" 2>/dev/null)"
  [[ "$node_cluster" == "$CLUSTER_NAME" ]] || {
    echo "Cleanup node label did not match the bounded cluster" >&2
    return 1
  }
  mount_source="$(docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/var/lib/benchhandoff-e2e"}}{{println .Source}}{{end}}{{end}}' \
    "$cleanup_node" 2>/dev/null)"
  mount_source="${mount_source%$'\r'}"
  mount_source="${mount_source%$'\n'}"
  [[ "$mount_source" == "$DATA_ROOT" ]] || {
    echo "Cleanup mount did not match the bounded data root" >&2
    return 1
  }

  current_context="$("$KUBECTL" config current-context 2>/dev/null)"
  [[ "$current_context" == "kind-$CLUSTER_NAME" ]] || {
    echo "Cleanup kubectl context did not match the bounded cluster" >&2
    return 1
  }

  # Quiesce every component that can still write the hostPath before changing
  # ownership. This prevents a live UID-65532 runner from creating new files
  # after the ownership restoration but before the kind node is removed.
  system_namespace_ref="$("$KUBECTL" get namespace "$SYSTEM_NAMESPACE" \
    --ignore-not-found \
    --request-timeout=10s \
    -o name 2>/dev/null)" || {
    echo "System namespace cleanup query failed" >&2
    return 1
  }
  if [[ -n "$system_namespace_ref" ]]; then
    [[ "$system_namespace_ref" == "namespace/$SYSTEM_NAMESPACE" ]] || {
      echo "System namespace cleanup query returned an unexpected resource" >&2
      return 1
    }
    manager_deployment_ref="$("$KUBECTL" get deployment agentrun-controller \
      -n "$SYSTEM_NAMESPACE" \
      --ignore-not-found \
      --request-timeout=10s \
      -o name 2>/dev/null)" || {
      echo "Controller Deployment cleanup query failed" >&2
      return 1
    }
  else
    manager_deployment_ref=""
  fi
  if [[ -n "$manager_deployment_ref" ]]; then
    [[ "$manager_deployment_ref" == "deployment.apps/agentrun-controller" ]] || {
      echo "Controller Deployment cleanup query returned an unexpected resource" >&2
      return 1
    }
    "$KUBECTL" scale deployment/agentrun-controller \
      -n "$SYSTEM_NAMESPACE" \
      --replicas=0 \
      --request-timeout=10s >/dev/null 2>&1 || return 1
    manager_stop_deadline=$((SECONDS + 60))
    while true; do
      manager_pods="$("$KUBECTL" get pods \
        -n "$SYSTEM_NAMESPACE" \
        -l app.kubernetes.io/name=agentrun-controller \
        --request-timeout=10s \
        -o name 2>/dev/null)" || return 1
      if [[ -z "$manager_pods" ]]; then
        break
      fi
      if (( SECONDS >= manager_stop_deadline )); then
        echo "Controller Pods did not stop before cleanup" >&2
        return 1
      fi
      sleep 1
    done
  fi
  test_namespace_ref="$("$KUBECTL" get namespace "$TEST_NAMESPACE" \
    --ignore-not-found \
    --request-timeout=10s \
    -o name 2>/dev/null)" || {
    echo "Test namespace cleanup query failed" >&2
    return 1
  }
  if [[ -n "$test_namespace_ref" ]]; then
    [[ "$test_namespace_ref" == "namespace/$TEST_NAMESPACE" ]] || {
      echo "Test namespace cleanup query returned an unexpected resource" >&2
      return 1
    }
    "$KUBECTL" delete jobs,pods --all \
      -n "$TEST_NAMESPACE" \
      --ignore-not-found \
      --wait=true \
      --timeout=90s \
      --request-timeout=10s >/dev/null 2>&1 || return 1
    remaining_pods="$("$KUBECTL" get pods \
      -n "$TEST_NAMESPACE" \
      --request-timeout=10s \
      -o name 2>/dev/null)" || return 1
    [[ -z "$remaining_pods" ]] || {
      echo "Runner Pods remained after bounded quiescence" >&2
      return 1
    }
  fi

  docker exec "$cleanup_node" chown -hR "$HOST_UID:$HOST_GID" \
    /var/lib/benchhandoff-e2e >/dev/null 2>&1 || return 1
  docker exec "$cleanup_node" chmod -R u+rwX \
    /var/lib/benchhandoff-e2e >/dev/null 2>&1 || return 1
  return 0
}

cleanup_incomplete_evidence() {
  local entry name
  [[ "$EVIDENCE_DIR_OWNED" == "yes" && "$EVIDENCE_COMPLETE" != "yes" ]] ||
    return 0
  case "$EVIDENCE_DIR" in
    "$TEMP_PARENT"/*)
      ;;
    *)
      return 1
      ;;
  esac
  [[ -d "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] || return 1
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    name="$(basename -- "$entry")"
    case "$name" in
      takeover-evidence.json|SHA256SUMS)
        [[ -f "$entry" && ! -L "$entry" ]] || return 1
        rm -f -- "$entry" || return 1
        ;;
      *)
        return 1
        ;;
    esac
  done < <(find "$EVIDENCE_DIR" -mindepth 1 -maxdepth 1 -print)
  rmdir -- "$EVIDENCE_DIR" || return 1
  [[ ! -e "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] || return 1
  EVIDENCE_DIR_OWNED=""
}

cleanup() {
  exit_code="$?"
  set +e
  if [[ -n "$HAPPY_WATCH_PID" ]]; then
    kill "$HAPPY_WATCH_PID" >/dev/null 2>&1
    wait "$HAPPY_WATCH_PID" >/dev/null 2>&1
  fi
  if [[ "$exit_code" -ne 0 ]]; then
    bounded_diagnostics
  fi
  if [[ "$CONTROLLER_BUILD_DIR_OWNED" == "yes" ]]; then
    if [[ "$CONTROLLER_BUILD_DIR" != "$CONTROLLER_DIR/.build" ]] ||
      ! make --no-print-directory -C "$CONTROLLER_DIR" clean-e2e \
        >/dev/null 2>&1 ||
      [[ -e "$CONTROLLER_BUILD_DIR" || -L "$CONTROLLER_BUILD_DIR" ]]; then
      echo "Owned controller build-root cleanup failed" >&2
      exit_code=1
    fi
  fi
  if [[ "$exit_code" -ne 0 ]] && ! cleanup_incomplete_evidence; then
    echo "Incomplete E2E evidence cleanup failed; exact path retained:" >&2
    echo "  evidence=$EVIDENCE_DIR" >&2
    exit_code=1
  fi
  if ! restore_node_schedulability; then
    echo "Owned kind node schedulability restoration failed" >&2
    exit_code=1
  fi
  if restore_scratch_ownership; then
    resource_cleanup_ok="yes"
    if [[ "$REGISTRY_CREATED" == "yes" ]]; then
      if ! docker rm --force "$REGISTRY_NAME" >/dev/null 2>&1; then
        echo "Owned registry deletion failed" >&2
        resource_cleanup_ok=""
      fi
    fi
    if [[ "$CLUSTER_CREATED" == "yes" && -x "$KIND" ]]; then
      if ! "$KIND" delete cluster \
        --name "$CLUSTER_NAME" >/dev/null 2>&1; then
        echo "Owned cluster deletion failed" >&2
        resource_cleanup_ok=""
      fi
    fi
    if [[ "$REGISTRY_CREATED" == "yes" ]]; then
      registry_matches="$(docker ps --all \
        --filter "name=^/${REGISTRY_NAME}$" \
        --format '{{.Names}}' 2>/dev/null)"
      registry_query_status="$?"
      if [[ "$registry_query_status" -ne 0 ]]; then
        echo "Registry cleanup absence query failed" >&2
        resource_cleanup_ok=""
      elif [[ -n "$registry_matches" ]]; then
        echo "Owned registry remained after cleanup" >&2
        resource_cleanup_ok=""
      fi
    fi
    if [[ "$CLUSTER_CREATED" == "yes" && -x "$KIND" ]]; then
      cluster_list="$("$KIND" get clusters 2>/dev/null)"
      cluster_query_status="$?"
      if [[ "$cluster_query_status" -ne 0 ]]; then
        echo "Cluster cleanup absence query failed" >&2
        resource_cleanup_ok=""
      elif printf '%s\n' "$cluster_list" | grep -Fxq "$CLUSTER_NAME"; then
        echo "Owned cluster remained after cleanup" >&2
        resource_cleanup_ok=""
      fi
    fi
    if [[ "$resource_cleanup_ok" == "yes" ]]; then
      rm -rf -- "$SCRATCH_ROOT" >/dev/null 2>&1
      if [[ -e "$SCRATCH_ROOT" ]]; then
        echo "Scratch cleanup absence check failed" >&2
        exit_code=1
      fi
    else
      echo "Resource cleanup verification failed; scratch was retained:" >&2
      echo "  cluster=$CLUSTER_NAME" >&2
      echo "  registry=$REGISTRY_NAME" >&2
      echo "  scratch=$SCRATCH_ROOT" >&2
      exit_code=1
    fi
  else
    echo "Cleanup ownership check failed; bounded resources were retained:" >&2
    echo "  cluster=$CLUSTER_NAME" >&2
    echo "  registry=$REGISTRY_NAME" >&2
    echo "  scratch=$SCRATCH_ROOT" >&2
    exit_code=1
  fi
  exit "$exit_code"
}
trap cleanup EXIT
prepare_evidence_directory

download_verified() {
  url="$1"
  expected_sha256="$2"
  destination="$3"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --retry 5 --retry-all-errors --silent --show-error \
    --output "$destination" "$url"
  actual_sha256="$(sha256sum "$destination" | cut -d' ' -f1)"
  [[ "$actual_sha256" == "$expected_sha256" ]] ||
    fail "downloaded tool failed its registered SHA-256 check"
  echo "verified one pinned tool binary"
  chmod 0555 "$destination"
}

wait_for_phase() {
  name="$1"
  expected="$2"
  timeout_seconds="$3"
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    phase="$("$KUBECTL" get agentrun "$name" -n "$TEST_NAMESPACE" \
      -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == "$expected" ]]; then
      return 0
    fi
    if [[ "$phase" == "Blocked" && "$expected" != "Blocked" ]]; then
      fail "AgentRun $name blocked while waiting for $expected"
    fi
    sleep 2
  done
  fail "timed out waiting for AgentRun $name phase $expected"
}

wait_for_action() {
  name="$1"
  expected="$2"
  timeout_seconds="$3"
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    action="$("$KUBECTL" get agentrun "$name" -n "$TEST_NAMESPACE" \
      -o jsonpath='{.status.activeJobRef.action}' 2>/dev/null || true)"
    if [[ "$action" == "$expected" ]]; then
      return 0
    fi
    sleep 1
  done
  fail "timed out waiting for AgentRun $name action $expected"
}

run_uid() {
  "$KUBECTL" get agentrun "$1" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.metadata.uid}'
}

job_count() {
  uid="$1"
  action="$2"
  items="$("$KUBECTL" get jobs -n "$TEST_NAMESPACE" \
    -l "$LABEL_RUN_UID=$uid,$LABEL_ACTION=$action" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')"
  if [[ -z "$items" ]]; then
    printf '0\n'
  else
    printf '%s\n' "$items" | sed '/^$/d' | wc -l | tr -d ' '
  fi
}

pod_names() {
  uid="$1"
  action="$2"
  "$KUBECTL" get pods -n "$TEST_NAMESPACE" \
    -l "$LABEL_RUN_UID=$uid,$LABEL_ACTION=$action" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
}

single_pod_name() {
  uid="$1"
  action="$2"
  names="$(pod_names "$uid" "$action" | sed '/^$/d')"
  count="$(printf '%s\n' "$names" | sed '/^$/d' | wc -l | tr -d ' ')"
  [[ "$count" == "1" ]] || fail "expected one $action Pod, observed $count"
  printf '%s\n' "$names"
}

wait_for_single_pod() {
  uid="$1"
  action="$2"
  timeout_seconds="$3"
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    names="$(pod_names "$uid" "$action" | sed '/^$/d')"
    count="$(printf '%s\n' "$names" | sed '/^$/d' | wc -l | tr -d ' ')"
    if [[ "$count" == "1" ]]; then
      printf '%s\n' "$names"
      return 0
    fi
    if (( count > 1 )); then
      fail "expected one $action Pod while waiting, observed $count"
    fi
    sleep 1
  done
  fail "timed out waiting for one $action Pod"
}

assert_job_live() {
  job_name="$1"
  pod_name="$2"
  started_at="$("$KUBECTL" get pod "$pod_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="runner")].state.running.startedAt}')"
  [[ -n "$started_at" ]] ||
    fail "runner Pod was not live at the registered restart boundary"
  terminal_conditions="$("$KUBECTL" get job "$job_name" -n "$TEST_NAMESPACE" -o json |
    jq -r '
      [
        .status.conditions[]?
        | select((.type == "Complete" or .type == "Failed") and .status == "True")
      ]
      | length
    ')"
  [[ "$terminal_conditions" == "0" ]] ||
    fail "runner Job was already terminal at the registered restart boundary"
}

assert_two_ready_managers() {
  local pods_json total ready desired available
  pods_json="$("$KUBECTL" get pods \
    -n "$SYSTEM_NAMESPACE" \
    -l app.kubernetes.io/name=agentrun-controller \
    -o json)"
  total="$(jq -r '.items | length' <<<"$pods_json")"
  ready="$(jq -r '
    [
      .items[]
      | select(
          any(
            .status.conditions[]?;
            .type == "Ready" and .status == "True"
          )
        )
    ]
    | length
  ' <<<"$pods_json")"
  desired="$("$KUBECTL" get deployment agentrun-controller \
    -n "$SYSTEM_NAMESPACE" \
    -o jsonpath='{.spec.replicas}')"
  available="$("$KUBECTL" get deployment agentrun-controller \
    -n "$SYSTEM_NAMESPACE" \
    -o jsonpath='{.status.availableReplicas}')"
  [[ "$desired" == "2" && "$available" == "2" &&
    "$total" == "2" && "$ready" == "2" ]] ||
    fail "manager Deployment was not exactly two desired, available, and Ready Pods"
}

capture_stable_manager_pair() {
  local lease_before lease_after pods_json
  local resource_version_before resource_version_after holder_record passive_record
  local stable_snapshot=""

  for _ in $(seq 1 30); do
    lease_before="$("$KUBECTL" get lease "$LEASE_NAME" \
      -n "$SYSTEM_NAMESPACE" \
      -o json)"
    pods_json="$("$KUBECTL" get pods \
      -n "$SYSTEM_NAMESPACE" \
      -l app.kubernetes.io/name=agentrun-controller \
      -o json)"
    lease_after="$("$KUBECTL" get lease "$LEASE_NAME" \
      -n "$SYSTEM_NAMESPACE" \
      -o json)"
    resource_version_before="$(jq -er '.metadata.resourceVersion' \
      <<<"$lease_before")"
    resource_version_after="$(jq -er '.metadata.resourceVersion' \
      <<<"$lease_after")"
    if [[ "$resource_version_before" == "$resource_version_after" ]]; then
      stable_snapshot="yes"
      break
    fi
    sleep 0.2
  done
  [[ "$stable_snapshot" == "yes" ]] ||
    fail "could not bind one manager-pair snapshot to a stable Lease version"

  LEASE_HOLDER_OBSERVED="$(jq -er '.spec.holderIdentity' <<<"$lease_after")"
  LEASE_TRANSITIONS_OBSERVED="$(jq -er '.spec.leaseTransitions' \
    <<<"$lease_after")"
  LEASE_RESOURCE_VERSION_OBSERVED="$resource_version_after"
  [[ "$LEASE_TRANSITIONS_OBSERVED" =~ ^[0-9]+$ ]] ||
    fail "leader-election Lease transition count was not numeric"
  [[ "$(jq -er '.spec.leaseDurationSeconds' <<<"$lease_after")" == "15" ]] ||
    fail "leader-election Lease duration was not 15 seconds"

  [[ "$(jq -r '.items | length' <<<"$pods_json")" == "2" ]] ||
    fail "stable manager snapshot did not contain exactly two Pods"
  [[ "$(jq -r '
    [
      .items[]
      | select(
          .metadata.deletionTimestamp == null
          and .status.phase == "Running"
          and any(
            .status.conditions[]?;
            .type == "Ready" and .status == "True"
          )
        )
    ]
    | length
  ' <<<"$pods_json")" == "2" ]] ||
    fail "stable manager snapshot did not contain two running Ready Pods"

  holder_record="$(jq -cer \
    --arg holder "$LEASE_HOLDER_OBSERVED" '
      [
        .items[]
        | .metadata.name as $name
        | select($holder | startswith($name + "_"))
        | {name: .metadata.name, uid: .metadata.uid}
      ]
      | select(length == 1)
      | .[0]
    ' <<<"$pods_json")" ||
    fail "Lease holder did not map to exactly one manager in the stable snapshot"
  LEASE_MANAGER_OBSERVED="$(jq -er '.name' <<<"$holder_record")"
  LEASE_MANAGER_UID_OBSERVED="$(jq -er '.uid' <<<"$holder_record")"

  passive_record="$(jq -cer \
    --arg active "$LEASE_MANAGER_OBSERVED" '
      [.items[] | select(.metadata.name != $active) |
        {name: .metadata.name, uid: .metadata.uid}]
      | select(length == 1)
      | .[0]
    ' <<<"$pods_json")" ||
    fail "stable manager snapshot did not contain one passive Pod"
  PASSIVE_MANAGER_OBSERVED="$(jq -er '.name' <<<"$passive_record")"
  PASSIVE_MANAGER_UID_OBSERVED="$(jq -er '.uid' <<<"$passive_record")"
  [[ "$LEASE_MANAGER_UID_OBSERVED" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ &&
    "$PASSIVE_MANAGER_UID_OBSERVED" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ &&
    "$LEASE_MANAGER_UID_OBSERVED" != "$PASSIVE_MANAGER_UID_OBSERVED" ]] ||
    fail "manager pair did not expose two distinct canonical Pod UIDs"
}

wait_for_preexisting_passive_takeover() {
  local previous_holder="$1" previous_transitions="$2"
  local passive_name="$3" passive_uid="$4" timeout_seconds="$5"
  local deadline lease_json holder transitions resource_version current_uid
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    lease_json="$("$KUBECTL" get lease "$LEASE_NAME" \
      -n "$SYSTEM_NAMESPACE" \
      -o json 2>/dev/null || true)"
    if [[ -n "$lease_json" ]]; then
      holder="$(jq -r '.spec.holderIdentity // empty' <<<"$lease_json")"
      transitions="$(jq -r '.spec.leaseTransitions // empty' <<<"$lease_json")"
      resource_version="$(jq -r '.metadata.resourceVersion // empty' \
        <<<"$lease_json")"
      if [[ "$holder" == "$passive_name"_* &&
        "$holder" != "$previous_holder" &&
        "$transitions" =~ ^[0-9]+$ &&
        "$transitions" -eq $((previous_transitions + 1)) &&
        -n "$resource_version" ]]; then
        current_uid="$("$KUBECTL" get pod "$passive_name" \
          -n "$SYSTEM_NAMESPACE" \
          -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
        if [[ "$current_uid" == "$passive_uid" ]]; then
          LEASE_HOLDER_OBSERVED="$holder"
          LEASE_TRANSITIONS_OBSERVED="$transitions"
          LEASE_RESOURCE_VERSION_OBSERVED="$resource_version"
          LEASE_MANAGER_OBSERVED="$passive_name"
          LEASE_MANAGER_UID_OBSERVED="$passive_uid"
          return 0
        fi
      fi
    fi
    sleep 1
  done
  fail "pre-existing passive manager did not acquire one newer Lease version"
}

perform_manager_takeover() {
  local action="$1" run_uid="$2" job_name="$3" job_uid="$4"
  local pod_name="$5" pod_uid="$6"
  local live_job_uid live_pod_uid live_pod_name
  local passive_name passive_uid
  local delete_lease_json delete_holder delete_transitions delete_resource_version
  local deletion_deadline current_deleted_uid deleted=""
  local job_count_before pod_count_before job_count_after pod_count_after

  assert_two_ready_managers
  assert_job_ref "$action" "$job_name" "$job_uid"
  live_pod_uid="$("$KUBECTL" get pod "$pod_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')"
  [[ "$live_pod_uid" == "$pod_uid" ]] ||
    fail "$action Pod UID changed before manager takeover"
  job_count_before="$(job_count "$run_uid" "$action")"
  pod_count_before="$(pod_names "$run_uid" "$action" |
    sed '/^$/d' | wc -l | tr -d ' ')"
  [[ "$job_count_before" == "1" && "$pod_count_before" == "1" ]] ||
    fail "$action did not begin takeover with one Job and one Pod"
  TAKEOVER_JOB_COUNT_BEFORE="$job_count_before"
  TAKEOVER_POD_COUNT_BEFORE="$pod_count_before"
  assert_job_live "$job_name" "$pod_name"

  [[ "$("$KUBECTL" get node "$KIND_NODE_NAME" \
    -o jsonpath='{.metadata.uid}')" == "$KIND_NODE_UID" ]] ||
    fail "kind node UID changed before the bounded cordon"
  NODE_CORDONED="yes"
  "$KUBECTL" cordon "$KIND_NODE_NAME" >/dev/null
  [[ "$("$KUBECTL" get node "$KIND_NODE_NAME" \
    -o jsonpath='{.spec.unschedulable}')" == "true" ]] ||
    fail "kind node was not cordoned before manager deletion"

  capture_stable_manager_pair
  TAKEOVER_HOLDER_BEFORE="$LEASE_HOLDER_OBSERVED"
  TAKEOVER_TRANSITIONS_BEFORE="$LEASE_TRANSITIONS_OBSERVED"
  TAKEOVER_MANAGER_BEFORE="$LEASE_MANAGER_OBSERVED"
  TAKEOVER_MANAGER_BEFORE_UID="$LEASE_MANAGER_UID_OBSERVED"
  TAKEOVER_LEASE_RESOURCE_VERSION_BEFORE="$LEASE_RESOURCE_VERSION_OBSERVED"
  passive_name="$PASSIVE_MANAGER_OBSERVED"
  passive_uid="$PASSIVE_MANAGER_UID_OBSERVED"
  TAKEOVER_PASSIVE_BEFORE="$passive_name"
  TAKEOVER_PASSIVE_BEFORE_UID="$passive_uid"

  delete_lease_json="$("$KUBECTL" get lease "$LEASE_NAME" \
    -n "$SYSTEM_NAMESPACE" \
    -o json)"
  delete_holder="$(jq -er '.spec.holderIdentity' <<<"$delete_lease_json")"
  delete_transitions="$(jq -er '.spec.leaseTransitions' <<<"$delete_lease_json")"
  delete_resource_version="$(jq -er '.metadata.resourceVersion' \
    <<<"$delete_lease_json")"
  [[ "$delete_holder" == "$TAKEOVER_HOLDER_BEFORE" &&
    "$delete_transitions" == "$TAKEOVER_TRANSITIONS_BEFORE" &&
    "$delete_resource_version" == "$TAKEOVER_LEASE_RESOURCE_VERSION_BEFORE" ]] ||
    fail "Lease changed after the registered pre-delete snapshot"
  [[ "$("$KUBECTL" get pod "$TAKEOVER_MANAGER_BEFORE" \
    -n "$SYSTEM_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')" == "$TAKEOVER_MANAGER_BEFORE_UID" ]] ||
    fail "Lease-holder manager UID changed before its bounded deletion"
  jq -cn \
    --arg uid "$TAKEOVER_MANAGER_BEFORE_UID" \
    '{
      apiVersion: "v1",
      kind: "DeleteOptions",
      preconditions: {uid: $uid},
      propagationPolicy: "Background"
    }' |
    "$KUBECTL" delete \
      --raw "/api/v1/namespaces/$SYSTEM_NAMESPACE/pods/$TAKEOVER_MANAGER_BEFORE" \
      -f - \
      --request-timeout=15s >/dev/null
  deletion_deadline=$((SECONDS + 60))
  while (( SECONDS < deletion_deadline )); do
    current_deleted_uid="$("$KUBECTL" get pod "$TAKEOVER_MANAGER_BEFORE" \
      -n "$SYSTEM_NAMESPACE" \
      -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    if [[ -z "$current_deleted_uid" ]]; then
      deleted="yes"
      break
    fi
    [[ "$current_deleted_uid" == "$TAKEOVER_MANAGER_BEFORE_UID" ]] ||
      fail "deleted Lease-holder Pod name resolved to an unexpected UID"
    sleep 1
  done
  [[ "$deleted" == "yes" ]] ||
    fail "UID-preconditioned Lease-holder Pod deletion did not complete"
  wait_for_preexisting_passive_takeover \
    "$TAKEOVER_HOLDER_BEFORE" \
    "$TAKEOVER_TRANSITIONS_BEFORE" \
    "$passive_name" \
    "$passive_uid" \
    90
  TAKEOVER_HOLDER_AFTER="$LEASE_HOLDER_OBSERVED"
  TAKEOVER_TRANSITIONS_AFTER="$LEASE_TRANSITIONS_OBSERVED"
  TAKEOVER_MANAGER_AFTER="$LEASE_MANAGER_OBSERVED"
  TAKEOVER_MANAGER_AFTER_UID="$LEASE_MANAGER_UID_OBSERVED"
  TAKEOVER_LEASE_RESOURCE_VERSION_AFTER="$LEASE_RESOURCE_VERSION_OBSERVED"
  [[ "$TAKEOVER_MANAGER_AFTER" == "$passive_name" &&
    "$TAKEOVER_MANAGER_AFTER_UID" == "$passive_uid" ]] ||
    fail "takeover was not performed by the pre-existing passive manager"

  restore_node_schedulability ||
    fail "kind node could not be uncordoned after the registered takeover"
  "$KUBECTL" rollout status deployment/agentrun-controller \
    -n "$SYSTEM_NAMESPACE" \
    --timeout=90s
  assert_two_ready_managers
  [[ "$("$KUBECTL" get pod "$passive_name" \
    -n "$SYSTEM_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')" == "$passive_uid" ]] ||
    fail "pre-existing passive manager was replaced after acquiring the Lease"

  live_job_uid="$("$KUBECTL" get job "$job_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')"
  live_pod_name="$(single_pod_name "$run_uid" "$action")"
  live_pod_uid="$("$KUBECTL" get pod "$live_pod_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')"
  [[ "$live_job_uid" == "$job_uid" ]] ||
    fail "$action Job UID changed across manager takeover"
  [[ "$live_pod_name" == "$pod_name" && "$live_pod_uid" == "$pod_uid" ]] ||
    fail "$action Pod identity changed across manager takeover"
  job_count_after="$(job_count "$run_uid" "$action")"
  pod_count_after="$(pod_names "$run_uid" "$action" |
    sed '/^$/d' | wc -l | tr -d ' ')"
  [[ "$job_count_after" == "1" && "$pod_count_after" == "1" ]] ||
    fail "$action takeover did not preserve exactly one Job and one Pod"
  TAKEOVER_JOB_COUNT_AFTER="$job_count_after"
  TAKEOVER_POD_COUNT_AFTER="$pod_count_after"
  TAKEOVER_JOB_AFTER_NAME="$job_name"
  TAKEOVER_JOB_AFTER_UID="$live_job_uid"
  TAKEOVER_POD_AFTER_NAME="$live_pod_name"
  TAKEOVER_POD_AFTER_UID="$live_pod_uid"
  assert_job_live "$job_name" "$pod_name"
}

release_runner_gate() {
  local pod_name="$1" marker="$2"
  case "$marker" in
    start.release|resume.release)
      ;;
    *)
      fail "refusing an unknown runner release marker"
      ;;
  esac
  "$KUBECTL" exec "$pod_name" \
    -n "$TEST_NAMESPACE" \
    -c runner \
    -- python -c \
    'import os,sys; from pathlib import Path; Path(os.environ["TMPDIR"], sys.argv[1]).touch()' \
    "$marker"
}

assert_lease_rbac() {
  local service_account verb answer
  service_account="system:serviceaccount:$SYSTEM_NAMESPACE:agentrun-controller"
  for verb in get update; do
    answer="$("$KUBECTL" auth can-i "$verb" \
      "leases.coordination.k8s.io/$LEASE_NAME" \
      --as="$service_account" \
      -n "$SYSTEM_NAMESPACE")"
    [[ "$answer" == "yes" ]] ||
      fail "manager ServiceAccount cannot $verb the exact leader-election Lease"
  done
  for verb in create list watch patch delete; do
    answer="$("$KUBECTL" auth can-i "$verb" leases.coordination.k8s.io \
      --as="$service_account" \
      -n "$SYSTEM_NAMESPACE")"
    [[ "$answer" == "no" ]] ||
      fail "manager ServiceAccount can unexpectedly $verb Leases"
  done
  answer="$("$KUBECTL" auth can-i update \
    leases.coordination.k8s.io/not-the-registered-lease \
    --as="$service_account" \
    -n "$SYSTEM_NAMESPACE")"
  [[ "$answer" == "no" ]] ||
    fail "manager ServiceAccount can update another Lease in the system namespace"
  answer="$("$KUBECTL" auth can-i get leases.coordination.k8s.io \
    --as="$service_account" \
    -n "$TEST_NAMESPACE")"
  [[ "$answer" == "no" ]] ||
    fail "manager ServiceAccount can read Leases outside the system namespace"
}

ready_reason() {
  "$KUBECTL" get agentrun "$1" -n "$TEST_NAMESPACE" -o json |
    jq -r '.status.conditions[] | select(.type == "Ready") | .reason' |
    tail -n 1
}

apply_case() {
  source_manifest="$1"
  rendered="$RENDERED_DIR/$(basename -- "$source_manifest")"
  sed \
    -e "s|WRONG_SUITE_SHA256|$WRONG_SUITE_SHA256|g" \
    -e "s|SUITE_SHA256|$SUITE_SHA256|g" \
    -e "s|RUNNER_IMAGE|$RUNNER_IMAGE|g" \
    "$source_manifest" > "$rendered"
  if grep -Eq '(RUNNER_IMAGE|SUITE_SHA256)' "$rendered"; then
    fail "rendered AgentRun retained an unresolved placeholder"
  fi
  "$KUBECTL" apply -f "$rendered"
}

workspace_tree_sha256() {
  tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
    --numeric-owner --format=ustar -cf - -C "$1" . |
    sha256sum | cut -d' ' -f1
}

assert_job_ref() {
  action="$1"
  job_name="$2"
  job_uid="$3"
  [[ -n "$job_name" ]] ||
    fail "$action activeJobRef had no Job name"
  [[ "$job_uid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    fail "$action activeJobRef UID was not a canonical UUID"
  live_job_uid="$("$KUBECTL" get job "$job_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.metadata.uid}')"
  [[ "$live_job_uid" == "$job_uid" ]] ||
    fail "$action activeJobRef UID did not match the live Job"
}

stop_happy_watch() {
  kill "$HAPPY_WATCH_PID" >/dev/null 2>&1 || true
  wait "$HAPPY_WATCH_PID" >/dev/null 2>&1 || true
  HAPPY_WATCH_PID=""
}

assert_step_message() {
  pod_name="$1"
  expected_action="$2"
  expected_outcome="$3"
  message="$("$KUBECTL" get pod "$pod_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="runner")].state.terminated.message}')"
  jq -e \
    --arg action "$expected_action" \
    --arg outcome "$expected_outcome" \
    '
      (keys | sort) == [
        "action",
        "agent_run_uid",
        "bundle_sha256",
        "error_code",
        "execution_spec_sha256",
        "outcome",
        "protocol",
        "resume_decision_sha256",
        "run_id"
      ]
      and .protocol == "benchhandoff-controller-step/v1"
      and .action == $action
      and .outcome == $outcome
    ' <<<"$message" >/dev/null
}

assert_runner_image_id() {
  pod_name="$1"
  image_id="$("$KUBECTL" get pod "$pod_name" -n "$TEST_NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="runner")].imageID}')"
  observed_digest="${image_id##*@}"
  [[ "$observed_digest" == "$RUNNER_DIGEST" ]] ||
    fail "runner imageID did not match the pushed executable image digest"
}

bind_clean_source_revision

download_verified \
  "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64" \
  "$KIND_SHA256" \
  "$KIND"
download_verified \
  "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl" \
  "$KUBECTL_SHA256" \
  "$KUBECTL"

"$KIND" version
"$KUBECTL" version --client=true
go version
docker version

make --no-print-directory -C "$CONTROLLER_DIR" verify
[[ "$CONTROLLER_BUILD_DIR" == "$CONTROLLER_DIR/.build" &&
  ! -e "$CONTROLLER_BUILD_DIR" &&
  ! -L "$CONTROLLER_BUILD_DIR" ]] ||
  fail "controller build root existed before this E2E run"
CONTROLLER_BUILD_DIR_OWNED="yes"
make --no-print-directory -C "$CONTROLLER_DIR" image MANAGER_IMAGE="$MANAGER_IMAGE"

docker pull --platform linux/amd64 "$REGISTRY_IMAGE" >/dev/null
docker run --detach --restart=always --platform linux/amd64 \
  --publish 127.0.0.1::5000 \
  --name "$REGISTRY_NAME" \
  "$REGISTRY_IMAGE" >/dev/null
REGISTRY_CREATED="yes"
registry_port="$(docker inspect \
  --format '{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}' \
  "$REGISTRY_NAME")"
[[ "$registry_port" =~ ^[0-9]{2,5}$ ]] ||
  fail "local registry did not receive one bounded host port"
registry_ready=""
for _ in $(seq 1 30); do
  if curl --fail --silent \
    "http://127.0.0.1:$registry_port/v2/" >/dev/null 2>&1; then
    registry_ready="yes"
    break
  fi
  sleep 1
done
[[ "$registry_ready" == "yes" ]] ||
  fail "local registry did not become ready"

docker pull --platform linux/amd64 "$PYTHON_BASE_IMAGE" >/dev/null
runner_tag="localhost:$registry_port/benchhandoff-runner:e2e-$cluster_suffix"
make --no-print-directory -C "$CONTROLLER_DIR" runner-image \
  RUNNER_IMAGE="$runner_tag"
push_record="$SCRATCH_ROOT/runner-push.txt"
docker push "$runner_tag" >"$push_record" 2>&1
RUNNER_DIGEST="$(sed -n \
  's/^.*digest: \(sha256:[a-f0-9]\{64\}\).*$/\1/p' \
  "$push_record" | tail -n 1)"
[[ "$RUNNER_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] ||
  fail "local registry did not return one canonical runner digest"
repo_digests="$(docker image inspect "$runner_tag" \
  --format '{{range .RepoDigests}}{{println .}}{{end}}')"
[[ "$repo_digests" == *"@$RUNNER_DIGEST"* ]] ||
  fail "local Docker metadata did not bind the pushed runner digest"
RUNNER_IMAGE="localhost:$registry_port/benchhandoff-runner@$RUNNER_DIGEST"

mkdir -p -- "$DATA_ROOT/suites" "$DATA_ROOT/runs"
SUITE_SHA256="$(sha256sum "$FIXTURE_DIR/suite.toml" | cut -d' ' -f1)"
[[ "$SUITE_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
  fail "fixture suite did not produce one canonical SHA-256"
if [[ "${SUITE_SHA256:0:1}" == "0" ]]; then
  WRONG_SUITE_SHA256="1${SUITE_SHA256:1}"
else
  WRONG_SUITE_SHA256="0${SUITE_SHA256:1}"
fi
[[ "$WRONG_SUITE_SHA256" != "$SUITE_SHA256" ]] ||
  fail "wrong suite digest fixture did not differ"
for case_name in happy wrong-approval duplicate-pod wrong-suite-digest; do
  mkdir -p -- "$DATA_ROOT/suites/$case_name"
  cp -a -- "$FIXTURE_DIR/." "$DATA_ROOT/suites/$case_name/"
done
find "$DATA_ROOT/suites" -type f -exec chmod 0444 {} +
find "$DATA_ROOT/suites" -type d -exec chmod 0555 {} +
for case_name in happy wrong-approval duplicate-pod wrong-suite-digest; do
  chmod 1777 "$DATA_ROOT/suites/$case_name"
  chmod 0777 "$DATA_ROOT/suites/$case_name/workspace"
done
chmod 0755 "$DATA_ROOT"
chmod 0777 "$DATA_ROOT/runs"

kind_config="$RENDERED_DIR/kind.yaml"
sed \
  -e "s|HOST_DATA_ROOT|$DATA_ROOT|g" \
  "$MANIFEST_DIR/kind.yaml.tmpl" > "$kind_config"

CLUSTER_CREATE_ATTEMPTED="yes"
timeout --signal=TERM --kill-after=30s 5m \
  "$KIND" create cluster \
  --name "$CLUSTER_NAME" \
  --config "$kind_config" \
  --image "$KIND_NODE_IMAGE" \
  --wait 5m
CLUSTER_CREATED="yes"
registry_hosts="$RENDERED_DIR/registry-hosts.toml"
sed "s|REGISTRY_NAME|$REGISTRY_NAME|g" \
  "$MANIFEST_DIR/registry-hosts.toml.tmpl" > "$registry_hosts"
mapfile -t kind_nodes < <("$KIND" get nodes --name "$CLUSTER_NAME")
[[ "${#kind_nodes[@]}" == "1" ]] ||
  fail "pinned single-node kind cluster did not expose exactly one node"
KIND_NODE_NAME="${kind_nodes[0]}"
[[ "$KIND_NODE_NAME" == "$CLUSTER_NAME-control-plane" ]] ||
  fail "kind node name did not match the bounded cluster"
KIND_NODE_UID="$("$KUBECTL" get node "$KIND_NODE_NAME" \
  -o jsonpath='{.metadata.uid}')"
[[ "$KIND_NODE_UID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
  fail "kind node UID was not canonical"
registry_dir="/etc/containerd/certs.d/localhost:$registry_port"
for kind_node in "${kind_nodes[@]}"; do
  docker exec "$kind_node" mkdir -p "$registry_dir"
  docker cp "$registry_hosts" "$kind_node:$registry_dir/hosts.toml"
done
"$KIND" load docker-image "$MANAGER_IMAGE" --name "$CLUSTER_NAME"
docker network connect kind "$REGISTRY_NAME"
local_registry_manifest="$RENDERED_DIR/local-registry.yaml"
sed "s|REGISTRY_PORT|$registry_port|g" \
  "$MANIFEST_DIR/local-registry.yaml.tmpl" > "$local_registry_manifest"
"$KUBECTL" apply -f "$local_registry_manifest"

install_base="$RENDERED_DIR/install-base.yaml"
install_manifest="$RENDERED_DIR/install.yaml"
"$KUBECTL" kustomize \
  "$REPOSITORY_ROOT/controller/config/default" > "$install_base"
[[ "$(grep -c 'image: benchhandoff-agentrun-controller:e2e' "$install_base")" == "1" ]] ||
  fail "manager image placeholder was not unique"
sed "s|image: benchhandoff-agentrun-controller:e2e|image: $MANAGER_IMAGE|" \
  "$install_base" > "$install_manifest"
"$KUBECTL" apply -f "$install_manifest"
"$KUBECTL" wait --for=condition=Established \
  customresourcedefinition/agentruns.control.benchhandoff.dev \
  --timeout=60s
"$KUBECTL" rollout status deployment/agentrun-controller \
  -n "$SYSTEM_NAMESPACE" \
  --timeout=90s
assert_two_ready_managers
assert_lease_rbac
lease_ready=""
for _ in $(seq 1 60); do
  lease_holder="$("$KUBECTL" get lease "$LEASE_NAME" \
    -n "$SYSTEM_NAMESPACE" \
    -o jsonpath='{.spec.holderIdentity}' 2>/dev/null || true)"
  if [[ -n "$lease_holder" ]]; then
    lease_ready="yes"
    break
  fi
  sleep 1
done
[[ "$lease_ready" == "yes" ]] ||
  fail "leader-election Lease did not acquire an initial holder"
capture_stable_manager_pair
"$KUBECTL" apply -f "$MANIFEST_DIR/storage.yaml"
"$KUBECTL" wait --for=jsonpath='{.status.phase}'=Bound \
  persistentvolumeclaim/benchhandoff-e2e-data \
  -n "$TEST_NAMESPACE" \
  --timeout=60s

# A validly encoded but incorrect suite digest must fail in the one start
# runner before start_run can create a run or mutate the source workspace.
wrong_suite_workspace="$DATA_ROOT/suites/wrong-suite-digest/workspace"
wrong_suite_workspace_before="$(workspace_tree_sha256 "$wrong_suite_workspace")"
apply_case "$MANIFEST_DIR/wrong-suite-digest.yaml"
wait_for_phase wrong-suite-digest Blocked 90
wrong_suite_uid="$(run_uid wrong-suite-digest)"
[[ "$(ready_reason wrong-suite-digest)" == "JobFailed" ]] ||
  fail "wrong suite digest did not stop at JobFailed"
[[ "$(job_count "$wrong_suite_uid" start)" == "1" ]] ||
  fail "wrong suite digest did not preserve exactly one start Job"
wrong_suite_pod="$(single_pod_name "$wrong_suite_uid" start)"
wrong_suite_exit="$("$KUBECTL" get pod "$wrong_suite_pod" \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="runner")].state.terminated.exitCode}')"
[[ "$wrong_suite_exit" =~ ^[1-9][0-9]*$ ]] ||
  fail "wrong suite digest runner did not terminate nonzero"
assert_step_message "$wrong_suite_pod" start blocked
wrong_suite_spec_sha="$("$KUBECTL" get agentrun wrong-suite-digest \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.executionSpecSHA256}')"
[[ "$wrong_suite_spec_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "wrong suite digest case did not publish a canonical execution spec"
wrong_suite_message="$("$KUBECTL" get pod "$wrong_suite_pod" \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="runner")].state.terminated.message}')"
jq -e \
  --arg run_uid "$wrong_suite_uid" \
  --arg spec_sha "$wrong_suite_spec_sha" \
  '
    .agent_run_uid == $run_uid
    and .execution_spec_sha256 == $spec_sha
    and .error_code == "evidence_invalid"
    and .run_id == ""
    and .resume_decision_sha256 == ""
    and .bundle_sha256 == ""
  ' <<<"$wrong_suite_message" >/dev/null ||
  fail "wrong suite digest termination was not exact evidence-invalid protocol"
[[ ! -e "$DATA_ROOT/runs/$wrong_suite_uid" ]] ||
  fail "wrong suite digest created a run directory"
[[ "$(workspace_tree_sha256 "$wrong_suite_workspace")" == "$wrong_suite_workspace_before" ]] ||
  fail "wrong suite digest mutated the source workspace"
[[ "$(job_count "$wrong_suite_uid" resume)" == "0" ]]
[[ "$(job_count "$wrong_suite_uid" verify)" == "0" ]]

# Happy path: delete the exact Lease holder while start and resume are live,
# require active/passive takeover without changing either runner identity,
# then require the distinct verify Job and Pod.
apply_case "$MANIFEST_DIR/happy.yaml"
wait_for_action happy start 60
happy_uid="$(run_uid happy)"
start_job="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.activeJobRef.name}')"
start_job_uid="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.activeJobRef.uid}')"
assert_job_ref start "$start_job" "$start_job_uid"
happy_start_pod="$(wait_for_single_pod "$happy_uid" start 45)"
"$KUBECTL" wait --for=condition=Ready pod/"$happy_start_pod" \
  -n "$TEST_NAMESPACE" \
  --timeout=45s
assert_job_live "$start_job" "$happy_start_pod"
start_pod_uid="$("$KUBECTL" get pod "$happy_start_pod" \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.uid}')"
perform_manager_takeover \
  start \
  "$happy_uid" \
  "$start_job" \
  "$start_job_uid" \
  "$happy_start_pod" \
  "$start_pod_uid"
start_holder_before="$TAKEOVER_HOLDER_BEFORE"
start_holder_after="$TAKEOVER_HOLDER_AFTER"
start_manager_before="$TAKEOVER_MANAGER_BEFORE"
start_manager_after="$TAKEOVER_MANAGER_AFTER"
start_manager_before_uid="$TAKEOVER_MANAGER_BEFORE_UID"
start_manager_after_uid="$TAKEOVER_MANAGER_AFTER_UID"
start_passive_before="$TAKEOVER_PASSIVE_BEFORE"
start_passive_before_uid="$TAKEOVER_PASSIVE_BEFORE_UID"
start_transitions_before="$TAKEOVER_TRANSITIONS_BEFORE"
start_transitions_after="$TAKEOVER_TRANSITIONS_AFTER"
start_lease_resource_version_before="$TAKEOVER_LEASE_RESOURCE_VERSION_BEFORE"
start_lease_resource_version_after="$TAKEOVER_LEASE_RESOURCE_VERSION_AFTER"
start_job_after="$TAKEOVER_JOB_AFTER_NAME"
start_job_uid_after="$TAKEOVER_JOB_AFTER_UID"
start_pod_after="$TAKEOVER_POD_AFTER_NAME"
start_pod_uid_after="$TAKEOVER_POD_AFTER_UID"
start_job_count_before="$TAKEOVER_JOB_COUNT_BEFORE"
start_job_count_after="$TAKEOVER_JOB_COUNT_AFTER"
start_pod_count_before="$TAKEOVER_POD_COUNT_BEFORE"
start_pod_count_after="$TAKEOVER_POD_COUNT_AFTER"
release_runner_gate "$happy_start_pod" start.release
wait_for_phase happy AwaitingApproval 150
[[ "$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" -o jsonpath='{.status.activeJobRef.name}')" == "$start_job" ]]
[[ "$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" -o jsonpath='{.status.activeJobRef.uid}')" == "$start_job_uid" ]]
[[ "$(job_count "$happy_uid" resume)" == "0" ]]

decision="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.resumeDecisionSHA256}')"
[[ "$decision" =~ ^[0-9a-f]{64}$ ]] ||
  fail "controller did not publish a canonical resume decision"

happy_watch="$RENDERED_DIR/happy-active-refs.log"
timeout --signal=TERM --kill-after=5s 180s \
  "$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" --watch \
  -o 'jsonpath={.status.activeJobRef.action}{"|"}{.status.activeJobRef.name}{"|"}{.status.activeJobRef.uid}{"\n"}' \
  > "$happy_watch" 2>/dev/null &
HAPPY_WATCH_PID="$!"
watch_ready=""
for _ in $(seq 1 100); do
  if grep -Fxq "start|$start_job|$start_job_uid" "$happy_watch"; then
    watch_ready="yes"
    break
  fi
  if ! kill -0 "$HAPPY_WATCH_PID" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
[[ "$watch_ready" == "yes" ]] ||
  fail "activeJobRef watch did not establish at the start binding"

"$KUBECTL" patch agentrun happy -n "$TEST_NAMESPACE" --type=merge \
  -p "{\"spec\":{\"resumeDecisionSHA256\":\"$decision\"}}"
wait_for_action happy resume 60
resume_job="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.activeJobRef.name}')"
resume_job_uid="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.activeJobRef.uid}')"
assert_job_ref resume "$resume_job" "$resume_job_uid"
happy_resume_pod="$(wait_for_single_pod "$happy_uid" resume 45)"
"$KUBECTL" wait --for=condition=Ready pod/"$happy_resume_pod" \
  -n "$TEST_NAMESPACE" \
  --timeout=45s
resume_pod_uid="$("$KUBECTL" get pod "$happy_resume_pod" \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.uid}')"
perform_manager_takeover \
  resume \
  "$happy_uid" \
  "$resume_job" \
  "$resume_job_uid" \
  "$happy_resume_pod" \
  "$resume_pod_uid"
resume_holder_before="$TAKEOVER_HOLDER_BEFORE"
resume_holder_after="$TAKEOVER_HOLDER_AFTER"
resume_manager_before="$TAKEOVER_MANAGER_BEFORE"
resume_manager_after="$TAKEOVER_MANAGER_AFTER"
resume_manager_before_uid="$TAKEOVER_MANAGER_BEFORE_UID"
resume_manager_after_uid="$TAKEOVER_MANAGER_AFTER_UID"
resume_passive_before="$TAKEOVER_PASSIVE_BEFORE"
resume_passive_before_uid="$TAKEOVER_PASSIVE_BEFORE_UID"
resume_transitions_before="$TAKEOVER_TRANSITIONS_BEFORE"
resume_transitions_after="$TAKEOVER_TRANSITIONS_AFTER"
resume_lease_resource_version_before="$TAKEOVER_LEASE_RESOURCE_VERSION_BEFORE"
resume_lease_resource_version_after="$TAKEOVER_LEASE_RESOURCE_VERSION_AFTER"
resume_job_after="$TAKEOVER_JOB_AFTER_NAME"
resume_job_uid_after="$TAKEOVER_JOB_AFTER_UID"
resume_pod_after="$TAKEOVER_POD_AFTER_NAME"
resume_pod_uid_after="$TAKEOVER_POD_AFTER_UID"
resume_job_count_before="$TAKEOVER_JOB_COUNT_BEFORE"
resume_job_count_after="$TAKEOVER_JOB_COUNT_AFTER"
resume_pod_count_before="$TAKEOVER_POD_COUNT_BEFORE"
resume_pod_count_after="$TAKEOVER_POD_COUNT_AFTER"
release_runner_gate "$happy_resume_pod" resume.release
wait_for_phase happy Succeeded 180
binding_watch_complete=""
for _ in $(seq 1 100); do
  if grep -Eq '^resume\|[^|]+\|[^|]+$' "$happy_watch" &&
    grep -Eq '^verify\|[^|]+\|[^|]+$' "$happy_watch"; then
    binding_watch_complete="yes"
    break
  fi
  sleep 0.1
done
[[ "$binding_watch_complete" == "yes" ]] ||
  fail "activeJobRef watch missed resume or verify scheduling"
stop_happy_watch
resume_binding="$(grep -E '^resume\|[^|]+\|[^|]+$' "$happy_watch" | tail -n 1)"
verify_binding="$(grep -E '^verify\|[^|]+\|[^|]+$' "$happy_watch" | tail -n 1)"
IFS='|' read -r resume_action watched_resume_job watched_resume_job_uid <<<"$resume_binding"
IFS='|' read -r verify_action verify_job verify_job_uid <<<"$verify_binding"
[[ "$resume_action" == "resume" && "$verify_action" == "verify" ]]
[[ "$watched_resume_job" == "$resume_job" &&
  "$watched_resume_job_uid" == "$resume_job_uid" ]] ||
  fail "activeJobRef watch changed the resume Job identity"
assert_job_ref resume "$resume_job" "$resume_job_uid"
assert_job_ref verify "$verify_job" "$verify_job_uid"
bundle_sha="$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.bundleSHA256}')"
[[ "$bundle_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "controller did not publish a canonical bundle digest"

if [[ "${decision:0:1}" == "0" ]]; then
  changed_approval="1${decision:1}"
else
  changed_approval="0${decision:1}"
fi
if "$KUBECTL" patch agentrun happy -n "$TEST_NAMESPACE" --type=merge \
  -p "{\"spec\":{\"resumeDecisionSHA256\":\"$changed_approval\"}}" \
  >/dev/null 2>&1; then
  fail "resume decision approval was not write-once"
fi
[[ "$("$KUBECTL" get agentrun happy -n "$TEST_NAMESPACE" \
  -o jsonpath='{.spec.resumeDecisionSHA256}')" == "$decision" ]] ||
  fail "rejected approval mutation changed the stored decision"

for action in start resume verify; do
  [[ "$(job_count "$happy_uid" "$action")" == "1" ]] ||
    fail "happy path did not preserve exactly one $action Job"
done
start_pod="$(single_pod_name "$happy_uid" start)"
resume_pod="$(single_pod_name "$happy_uid" resume)"
verify_pod="$(single_pod_name "$happy_uid" verify)"
[[ "$start_pod" == "$happy_start_pod" &&
  "$resume_pod" == "$happy_resume_pod" ]] ||
  fail "final runner names changed after the registered takeovers"
[[ "$("$KUBECTL" get pod "$start_pod" -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.uid}')" == "$start_pod_uid" &&
  "$("$KUBECTL" get pod "$resume_pod" -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.uid}')" == "$resume_pod_uid" ]] ||
  fail "final runner UIDs changed after the registered takeovers"
verify_pod_uid="$("$KUBECTL" get pod "$verify_pod" \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.uid}')"
[[ "$start_pod" != "$resume_pod" && "$start_pod" != "$verify_pod" && "$resume_pod" != "$verify_pod" ]] ||
  fail "start, resume, and verify must use distinct Pods"
assert_step_message "$start_pod" start awaiting_approval
assert_step_message "$resume_pod" resume completed
assert_step_message "$verify_pod" verify verified
assert_runner_image_id "$start_pod"
assert_runner_image_id "$resume_pod"
assert_runner_image_id "$verify_pod"

# Admission must reject a digest that differs from the observed decision.
apply_case "$MANIFEST_DIR/wrong-approval.yaml"
wait_for_action wrong-approval start 60
wrong_uid="$(run_uid wrong-approval)"
wrong_start_pod="$(wait_for_single_pod "$wrong_uid" start 45)"
"$KUBECTL" wait --for=condition=Ready pod/"$wrong_start_pod" \
  -n "$TEST_NAMESPACE" \
  --timeout=45s
release_runner_gate "$wrong_start_pod" start.release
wait_for_phase wrong-approval AwaitingApproval 150
wrong_decision="$("$KUBECTL" get agentrun wrong-approval -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.resumeDecisionSHA256}')"
[[ "$wrong_decision" =~ ^[0-9a-f]{64}$ ]] ||
  fail "wrong-approval case did not publish a canonical decision"
if [[ "${wrong_decision:0:1}" == "0" ]]; then
  wrong_approval="1${wrong_decision:1}"
else
  wrong_approval="0${wrong_decision:1}"
fi
wrong_generation="$("$KUBECTL" get agentrun wrong-approval -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.generation}')"
if "$KUBECTL" patch agentrun wrong-approval -n "$TEST_NAMESPACE" --type=merge \
  -p "{\"spec\":{\"resumeDecisionSHA256\":\"$wrong_approval\"}}" \
  >/dev/null 2>&1; then
  fail "wrong approval was not rejected by API admission"
fi
wait_for_phase wrong-approval AwaitingApproval 15
[[ "$(ready_reason wrong-approval)" == "ApprovalRequired" ]] ||
  fail "rejected wrong approval changed the ready reason"
[[ "$("$KUBECTL" get agentrun wrong-approval -n "$TEST_NAMESPACE" \
  -o jsonpath='{.spec.resumeDecisionSHA256}')" == "" ]] ||
  fail "rejected wrong approval changed the stored spec"
[[ "$("$KUBECTL" get agentrun wrong-approval -n "$TEST_NAMESPACE" \
  -o jsonpath='{.metadata.generation}')" == "$wrong_generation" ]] ||
  fail "rejected wrong approval changed the generation"
[[ "$("$KUBECTL" get agentrun wrong-approval -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.resumeDecisionSHA256}')" == "$wrong_decision" ]] ||
  fail "rejected wrong approval changed the observed decision"
[[ "$(job_count "$wrong_uid" start)" == "1" ]]
[[ "$(job_count "$wrong_uid" resume)" == "0" ]]
[[ "$(job_count "$wrong_uid" verify)" == "0" ]]

# Stop the manager, inject a second label-conflicting Pod, and require the
# restarted manager to refuse the ambiguous result set.
apply_case "$MANIFEST_DIR/duplicate-pod.yaml"
wait_for_action duplicate-pod start 60
duplicate_uid="$(run_uid duplicate-pod)"
original_pod="$(wait_for_single_pod "$duplicate_uid" start 45)"
duplicate_start_job="$("$KUBECTL" get agentrun duplicate-pod -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.activeJobRef.name}')"
"$KUBECTL" wait --for=condition=Ready pod/"$original_pod" \
  -n "$TEST_NAMESPACE" \
  --timeout=45s
"$KUBECTL" scale deployment/agentrun-controller \
  -n "$SYSTEM_NAMESPACE" \
  --replicas=0
"$KUBECTL" wait --for=delete pod \
  -n "$SYSTEM_NAMESPACE" \
  -l app.kubernetes.io/name=agentrun-controller \
  --timeout=60s
assert_job_live "$duplicate_start_job" "$original_pod"

duplicate_name="duplicate-pod-conflict"
"$KUBECTL" get pod "$original_pod" -n "$TEST_NAMESPACE" -o json |
  jq \
    --arg name "$duplicate_name" \
    '
      del(
        .metadata.annotations["batch.kubernetes.io/job-tracking"],
        .metadata.creationTimestamp,
        .metadata.finalizers,
        .metadata.generateName,
        .metadata.managedFields,
        .metadata.ownerReferences,
        .metadata.resourceVersion,
        .metadata.uid,
        .spec.nodeName,
        .status
      )
      | del(
        .metadata.labels["batch.kubernetes.io/controller-uid"],
        .metadata.labels["batch.kubernetes.io/job-name"],
        .metadata.labels["controller-uid"],
        .metadata.labels["job-name"]
      )
      | .metadata.name = $name
      | .spec.containers[0].command = ["python", "-c"]
      | .spec.containers[0].args = ["import time; time.sleep(60)"]
    ' > "$RENDERED_DIR/duplicate-pod.json"
"$KUBECTL" create -f "$RENDERED_DIR/duplicate-pod.json"
"$KUBECTL" wait --for=condition=Ready pod/"$duplicate_name" \
  -n "$TEST_NAMESPACE" \
  --timeout=45s
"$KUBECTL" scale deployment/agentrun-controller \
  -n "$SYSTEM_NAMESPACE" \
  --replicas=2
"$KUBECTL" rollout status deployment/agentrun-controller \
  -n "$SYSTEM_NAMESPACE" \
  --timeout=90s
assert_two_ready_managers
wait_for_phase duplicate-pod Blocked 120
[[ "$(ready_reason duplicate-pod)" == "AmbiguousPodSet" ]] ||
  fail "duplicate Pod did not stop at AmbiguousPodSet"
[[ "$(pod_names "$duplicate_uid" start | sed '/^$/d' | wc -l | tr -d ' ')" == "2" ]]
[[ "$(job_count "$duplicate_uid" resume)" == "0" ]]
[[ "$(job_count "$duplicate_uid" verify)" == "0" ]]

verify_bound_source_revision
execution_spec_sha="$("$KUBECTL" get agentrun happy \
  -n "$TEST_NAMESPACE" \
  -o jsonpath='{.status.executionSpecSHA256}')"
[[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ &&
  "$execution_spec_sha" =~ ^[0-9a-f]{64}$ ]] ||
  fail "takeover evidence identifiers were not canonical"

jq -n -S \
  --arg source_revision "$SOURCE_REVISION" \
  --arg run_uid "$happy_uid" \
  --arg execution_spec_sha "$execution_spec_sha" \
  --arg bundle_sha "$bundle_sha" \
  --arg start_job "$start_job" \
  --arg start_job_uid "$start_job_uid" \
  --arg start_job_after "$start_job_after" \
  --arg start_job_uid_after "$start_job_uid_after" \
  --arg start_pod "$start_pod" \
  --arg start_pod_uid "$start_pod_uid" \
  --arg start_pod_after "$start_pod_after" \
  --arg start_pod_uid_after "$start_pod_uid_after" \
  --arg start_holder_before "$start_holder_before" \
  --arg start_holder_after "$start_holder_after" \
  --arg start_manager_before "$start_manager_before" \
  --arg start_manager_after "$start_manager_after" \
  --arg start_manager_before_uid "$start_manager_before_uid" \
  --arg start_manager_after_uid "$start_manager_after_uid" \
  --arg start_passive_before "$start_passive_before" \
  --arg start_passive_before_uid "$start_passive_before_uid" \
  --arg start_lease_resource_version_before "$start_lease_resource_version_before" \
  --arg start_lease_resource_version_after "$start_lease_resource_version_after" \
  --argjson start_transitions_before "$start_transitions_before" \
  --argjson start_transitions_after "$start_transitions_after" \
  --argjson start_job_count_before "$start_job_count_before" \
  --argjson start_job_count_after "$start_job_count_after" \
  --argjson start_pod_count_before "$start_pod_count_before" \
  --argjson start_pod_count_after "$start_pod_count_after" \
  --arg resume_job "$resume_job" \
  --arg resume_job_uid "$resume_job_uid" \
  --arg resume_job_after "$resume_job_after" \
  --arg resume_job_uid_after "$resume_job_uid_after" \
  --arg resume_pod "$resume_pod" \
  --arg resume_pod_uid "$resume_pod_uid" \
  --arg resume_pod_after "$resume_pod_after" \
  --arg resume_pod_uid_after "$resume_pod_uid_after" \
  --arg resume_holder_before "$resume_holder_before" \
  --arg resume_holder_after "$resume_holder_after" \
  --arg resume_manager_before "$resume_manager_before" \
  --arg resume_manager_after "$resume_manager_after" \
  --arg resume_manager_before_uid "$resume_manager_before_uid" \
  --arg resume_manager_after_uid "$resume_manager_after_uid" \
  --arg resume_passive_before "$resume_passive_before" \
  --arg resume_passive_before_uid "$resume_passive_before_uid" \
  --arg resume_lease_resource_version_before "$resume_lease_resource_version_before" \
  --arg resume_lease_resource_version_after "$resume_lease_resource_version_after" \
  --argjson resume_transitions_before "$resume_transitions_before" \
  --argjson resume_transitions_after "$resume_transitions_after" \
  --argjson resume_job_count_before "$resume_job_count_before" \
  --argjson resume_job_count_after "$resume_job_count_after" \
  --argjson resume_pod_count_before "$resume_pod_count_before" \
  --argjson resume_pod_count_after "$resume_pod_count_after" \
  --arg verify_job "$verify_job" \
  --arg verify_job_uid "$verify_job_uid" \
  --arg verify_pod "$verify_pod" \
  --arg verify_pod_uid "$verify_pod_uid" \
  '{
    schema_version: 1,
    kind: "benchhandoff-agentrun-two-manager-takeover-evidence",
    source_revision: $source_revision,
    source_tree_clean: true,
    environment: {
      kind_version: "v0.32.0",
      kubernetes_version: "v1.36.1",
      node_count: 1,
      manager_replicas: 2,
      api_server_and_storage_available: true
    },
    coordination: {
      resource: "coordination.k8s.io/v1 Lease",
      namespace: "benchhandoff-system",
      name: "agentrun-controller.benchhandoff.dev",
      lease_duration_seconds: 15,
      renew_deadline_seconds: 10,
      retry_period_seconds: 2,
      release_on_cancel: false,
      lease_precreated: true,
      lease_get_permission: true,
      lease_update_permission: true,
      lease_create_permission: false,
      lease_list_permission: false,
      lease_watch_permission: false,
      lease_patch_permission: false,
      lease_delete_permission: false,
      cross_namespace_lease_read: false
    },
    agent_run: {
      name: "happy",
      uid: $run_uid,
      execution_spec_sha256: $execution_spec_sha,
      final_phase: "Succeeded",
      bundle_sha256: $bundle_sha
    },
    takeovers: [
      {
        action: "start",
        manager_before: {
          name: $start_manager_before,
          uid: $start_manager_before_uid
        },
        manager_after: {
          name: $start_manager_after,
          uid: $start_manager_after_uid
        },
        passive_before: {
          name: $start_passive_before,
          uid: $start_passive_before_uid
        },
        lease_before: {
          resource_version: $start_lease_resource_version_before,
          holder: $start_holder_before,
          transitions: $start_transitions_before
        },
        lease_after: {
          resource_version: $start_lease_resource_version_after,
          holder: $start_holder_after,
          transitions: $start_transitions_after
        },
        job_before: {name: $start_job, uid: $start_job_uid},
        job_after: {name: $start_job_after, uid: $start_job_uid_after},
        pod_before: {name: $start_pod, uid: $start_pod_uid},
        pod_after: {name: $start_pod_after, uid: $start_pod_uid_after},
        job_count_before: $start_job_count_before,
        job_count_after: $start_job_count_after,
        pod_count_before: $start_pod_count_before,
        pod_count_after: $start_pod_count_after
      },
      {
        action: "resume",
        manager_before: {
          name: $resume_manager_before,
          uid: $resume_manager_before_uid
        },
        manager_after: {
          name: $resume_manager_after,
          uid: $resume_manager_after_uid
        },
        passive_before: {
          name: $resume_passive_before,
          uid: $resume_passive_before_uid
        },
        lease_before: {
          resource_version: $resume_lease_resource_version_before,
          holder: $resume_holder_before,
          transitions: $resume_transitions_before
        },
        lease_after: {
          resource_version: $resume_lease_resource_version_after,
          holder: $resume_holder_after,
          transitions: $resume_transitions_after
        },
        job_before: {name: $resume_job, uid: $resume_job_uid},
        job_after: {name: $resume_job_after, uid: $resume_job_uid_after},
        pod_before: {name: $resume_pod, uid: $resume_pod_uid},
        pod_after: {name: $resume_pod_after, uid: $resume_pod_uid_after},
        job_count_before: $resume_job_count_before,
        job_count_after: $resume_job_count_after,
        pod_count_before: $resume_pod_count_before,
        pod_count_after: $resume_pod_count_after
      }
    ],
    final_actions: [
      {
        action: "start",
        job: {name: $start_job_after, uid: $start_job_uid_after},
        pod: {name: $start_pod_after, uid: $start_pod_uid_after}
      },
      {
        action: "resume",
        job: {name: $resume_job_after, uid: $resume_job_uid_after},
        pod: {name: $resume_pod_after, uid: $resume_pod_uid_after}
      },
      {
        action: "verify",
        job: {name: $verify_job, uid: $verify_job_uid},
        pod: {name: $verify_pod, uid: $verify_pod_uid}
      }
    ],
    retained_negative_controls: [
      "wrong-suite-digest",
      "wrong-approval",
      "duplicate-pod"
    ],
    boundary: {
      synthetic_evidence: true,
      fixed_single_node_kind: true,
      strict_fencing: false,
      network_partition_safety: false,
      arbitrary_pod_recovery: false,
      multi_node_or_multi_cluster: false,
      exactly_once: false,
      production_high_availability: false,
      independent_reproduction_or_adoption: false
    }
  }' > "$EVIDENCE_DIR/takeover-evidence.json"

jq -e '
  .kind == "benchhandoff-agentrun-two-manager-takeover-evidence"
  and .source_tree_clean
  and (.source_revision | test("^[0-9a-f]{40}$"))
  and (.agent_run.uid |
    test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
  and (.agent_run.execution_spec_sha256 | test("^[0-9a-f]{64}$"))
  and (.agent_run.bundle_sha256 | test("^[0-9a-f]{64}$"))
  and .agent_run.final_phase == "Succeeded"
  and (.takeovers | length) == 2
  and all(
    .takeovers[];
    . as $takeover
    | (.manager_before.name | test("^agentrun-controller-[a-z0-9-]{11,32}$"))
    and (.manager_after.name | test("^agentrun-controller-[a-z0-9-]{11,32}$"))
    and (.manager_before.uid |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
    and (.manager_after.uid |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
    and .manager_before.name != .manager_after.name
    and .manager_before.uid != .manager_after.uid
    and .passive_before == .manager_after
    and (.lease_before.holder |
      startswith($takeover.manager_before.name + "_"))
    and (.lease_after.holder |
      startswith($takeover.manager_after.name + "_"))
    and .lease_before.holder != .lease_after.holder
    and .lease_before.resource_version != .lease_after.resource_version
    and (.lease_before.resource_version | test("^[0-9]+$"))
    and (.lease_after.resource_version | test("^[0-9]+$"))
    and .lease_after.transitions == (.lease_before.transitions + 1)
    and (.job_before.name |
      test("^agentrun-(start|resume)-[0-9a-f]{16}$"))
    and (.job_before.uid |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
    and (.pod_before.name | startswith($takeover.job_before.name + "-"))
    and (.pod_before.uid |
      test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
    and .job_before == .job_after
    and .pod_before == .pod_after
    and .job_count_before == 1
    and .job_count_after == 1
    and .pod_count_before == 1
    and .pod_count_after == 1
  )
  and (.final_actions | map(.action) == ["start", "resume", "verify"])
  and .takeovers[0].job_after == .final_actions[0].job
  and .takeovers[0].pod_after == .final_actions[0].pod
  and .takeovers[1].job_after == .final_actions[1].job
  and .takeovers[1].pod_after == .final_actions[1].pod
  and (.boundary.production_high_availability | not)
  and (.boundary.exactly_once | not)
' "$EVIDENCE_DIR/takeover-evidence.json" >/dev/null ||
  fail "generated takeover evidence failed its exact relationship gate"

if grep -Eiq \
  '([A-Za-z]:\\|/(Users|home)/|@|https?://|token[[:space:]]*[:=]|secret[[:space:]]*[:=]|password[[:space:]]*[:=])' \
  "$EVIDENCE_DIR/takeover-evidence.json"; then
  fail "generated takeover evidence exposed a path, contact, URL, or credential shape"
fi
(
  cd -- "$EVIDENCE_DIR"
  sha256sum takeover-evidence.json > SHA256SUMS
  mapfile -t evidence_entries < <(
    find . -mindepth 1 -maxdepth 1 -printf '%P|%y\n' | sort
  )
  [[ "${#evidence_entries[@]}" == "2" &&
    "${evidence_entries[0]}" == "SHA256SUMS|f" &&
    "${evidence_entries[1]}" == "takeover-evidence.json|f" ]] ||
    fail "takeover evidence directory did not contain exactly two regular files"
  sha256sum --check --strict SHA256SUMS >/dev/null
)
[[ "$(wc -l < "$EVIDENCE_DIR/SHA256SUMS" | tr -d ' ')" == "1" ]] ||
  fail "takeover evidence checksum manifest was not singular"
python "$REPOSITORY_ROOT/tools/verify_public_privacy.py" \
  --evidence-dir "$EVIDENCE_DIR"
verify_bound_source_revision
EVIDENCE_COMPLETE="yes"

echo "PASS: real kind AgentRun two-manager Lease takeovers, exact runner identities, approval, verify, and fail-closed cases"
