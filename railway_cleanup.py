#!/usr/bin/env python3
"""
railway_cleanup.py — BugKSA Bot Railway Account Cleanup
========================================================
يقوم هذا السكريبت بـ:
  1. عرض جميع مشاريع Railway
  2. حذف المشاريع العشوائية وغير الضرورية
  3. التحقق من وجود مشروع bugksa-bot أو إنشاؤه
  4. التحقق من الخدمات والـ Volume والمتغيرات

الاستخدام:
  python railway_cleanup.py --token YOUR_RAILWAY_TOKEN

للحصول على الـ Token:
  https://railway.app/account/tokens → New Token
"""

import sys
import json
import time
import argparse
import urllib.request
import urllib.error

API_URL = "https://backboard.railway.app/graphql/v2"
GITHUB_REPO    = "mohammed8500/bugksa-bot"
PROJECT_NAME   = "bugksa-bot"
PROJECT_ID     = "97c65c33-2746-4be6-b541-2422dae46653"   # معرّف مشروع bugksa-bot
SERVICE_NAME   = "worker"
VOLUME_NAME    = "bot_data"
MOUNT_PATH     = "/app/data"
REQUIRED_VARS = [
    "OPENAI_API_KEY",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_SECRET",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def gql(token: str, query: str, variables: dict = None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[HTTP {e.code}] {body}")
        sys.exit(1)
    if "errors" in data:
        for err in data["errors"]:
            print(f"[GraphQL Error] {err.get('message')}")
        sys.exit(1)
    return data["data"]


def separator(title=""):
    print("\n" + "─" * 60)
    if title:
        print(f"  {title}")
        print("─" * 60)


# ── Queries / Mutations ────────────────────────────────────────────────────────

Q_ME = """
query {
  me { id name email }
}
"""

Q_PROJECTS = """
query {
  projects {
    edges {
      node {
        id
        name
        createdAt
        services {
          edges {
            node {
              id
              name
              source { repo }
            }
          }
        }
      }
    }
  }
}
"""

Q_ENV_VARS = """
query GetVars($serviceId: String!, $environmentId: String!) {
  variables(serviceId: $serviceId, environmentId: $environmentId)
}
"""

Q_ENVIRONMENTS = """
query GetEnvs($projectId: String!) {
  environments(projectId: $projectId) {
    edges {
      node { id name }
    }
  }
}
"""

Q_VOLUMES = """
query GetVolumes($projectId: String!) {
  volumes(projectId: $projectId) {
    edges {
      node {
        id
        name
        volumeInstances {
          edges {
            node {
              id
              mountPath
              serviceId
              environmentId
            }
          }
        }
      }
    }
  }
}
"""

M_DELETE_PROJECT = """
mutation DeleteProject($id: String!) {
  projectDelete(id: $id)
}
"""

M_CREATE_PROJECT = """
mutation CreateProject($name: String!) {
  projectCreate(input: { name: $name }) {
    id
    name
  }
}
"""

M_CREATE_SERVICE = """
mutation CreateService($projectId: String!, $name: String!, $repo: String!) {
  serviceCreate(input: {
    projectId: $projectId
    name: $name
    source: { repo: $repo }
  }) {
    id
    name
  }
}
"""

M_CREATE_VOLUME = """
mutation CreateVolume($projectId: String!, $name: String!, $mountPath: String!, $serviceId: String!, $environmentId: String!) {
  volumeCreate(input: {
    projectId: $projectId
    mountPath: $mountPath
    serviceId: $serviceId
    environmentId: $environmentId
  }) {
    id
    name
  }
}
"""

M_DELETE_SERVICE = """
mutation DeleteService($id: String!) {
  serviceDelete(id: $id)
}
"""

# ── Main Logic ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Railway BugKSA cleanup script")
    parser.add_argument("--token", required=True, help="Railway API token")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    args = parser.parse_args()

    token = args.token
    dry_run = args.dry_run

    if dry_run:
        print("[DRY RUN] لن يتم إجراء أي تغييرات فعلية")

    # ── 1. Auth check ──────────────────────────────────────────────────────────
    separator("1. التحقق من الـ Token")
    me = gql(token, Q_ME)["me"]
    print(f"✅ مسجّل دخول كـ: {me['name']} ({me['email']})")

    # ── 2. Audit all projects ──────────────────────────────────────────────────
    separator("2. قائمة جميع المشاريع")
    projects_data = gql(token, Q_PROJECTS)["projects"]["edges"]
    all_projects = [e["node"] for e in projects_data]

    print(f"إجمالي المشاريع: {len(all_projects)}\n")

    correct_project = None    # المشروع الصحيح bugksa-bot (معرّفه: PROJECT_ID)
    to_delete = []            # المشاريع المراد حذفها

    for p in all_projects:
        services = [e["node"] for e in p["services"]["edges"]]
        linked = any(
            (s.get("source") or {}).get("repo") == GITHUB_REPO
            for s in services
        )
        is_canonical = p["id"] == PROJECT_ID

        status = []
        if is_canonical:
            status.append("✅ المشروع الأساسي")
        elif p["name"].lower() == PROJECT_NAME.lower():
            status.append("اسم مطابق (نسخة مكررة)")
        elif linked:
            status.append(f"مرتبط بـ {GITHUB_REPO} (نسخة مكررة)")
        else:
            status.append("عشوائي")

        print(f"  [{p['id'][:8]}] {p['name']:35} | {', '.join(status)}")

        if is_canonical:
            correct_project = p
        else:
            to_delete.append(p)

    # ── 3. Determine canonical project ────────────────────────────────────────
    separator("3. تحديد المشروع الأساسي")

    if correct_project:
        print(f"✅ المشروع الأساسي: {correct_project['name']} [{correct_project['id']}]")
    else:
        print(f"⚠️  المشروع {PROJECT_ID} غير موجود في الحساب — سيتم إنشاؤه")

    # ── 4. Delete unwanted projects ────────────────────────────────────────────
    separator("4. حذف المشاريع غير الضرورية")

    if not to_delete:
        print("✅ لا توجد مشاريع زائدة للحذف")
    else:
        for p in to_delete:
            print(f"  🗑  حذف: {p['name']} [{p['id'][:8]}] ... ", end="", flush=True)
            if not dry_run:
                gql(token, M_DELETE_PROJECT, {"id": p["id"]})
                time.sleep(0.5)
                print("تم")
            else:
                print("(dry-run)")

    # ── 5. Create project if missing ───────────────────────────────────────────
    if correct_project is None:
        separator("5. إنشاء مشروع bugksa-bot")
        if not dry_run:
            result = gql(token, M_CREATE_PROJECT, {"name": PROJECT_NAME})
            correct_project = result["projectCreate"]
            print(f"✅ تم إنشاء المشروع: {correct_project['name']} [{correct_project['id'][:8]}]")
        else:
            print("(dry-run) سيتم إنشاء مشروع bugksa-bot")
            return  # can't continue without real project ID

    project_id = PROJECT_ID

    # Refresh project services after deletions
    fresh = gql(token, Q_PROJECTS)["projects"]["edges"]
    for e in fresh:
        if e["node"]["id"] == project_id:
            correct_project = e["node"]
            break

    # ── 6. Audit services ──────────────────────────────────────────────────────
    separator("6. مراجعة الخدمات داخل bugksa-bot")

    services = [e["node"] for e in correct_project["services"]["edges"]]
    worker_service = None
    services_to_delete = []

    for s in services:
        repo = (s.get("source") or {}).get("repo", "بدون مصدر")
        is_worker = s["name"].lower() == SERVICE_NAME.lower()
        print(f"  [{s['id'][:8]}] {s['name']:25} | repo: {repo}")
        if is_worker and worker_service is None:
            worker_service = s
        elif is_worker:
            services_to_delete.append(s)  # نسخة مكررة من worker
        elif not is_worker:
            services_to_delete.append(s)  # خدمة غير ضرورية

    # حذف الخدمات الزائدة
    for s in services_to_delete:
        print(f"  🗑  حذف خدمة: {s['name']} [{s['id'][:8]}] ... ", end="", flush=True)
        if not dry_run:
            gql(token, M_DELETE_SERVICE, {"id": s["id"]})
            print("تم")
        else:
            print("(dry-run)")

    # إنشاء worker إذا لم يكن موجوداً
    if worker_service is None:
        print(f"\n⚠️  خدمة worker غير موجودة — سيتم إنشاؤها")
        if not dry_run:
            result = gql(token, M_CREATE_SERVICE, {
                "projectId": project_id,
                "name": SERVICE_NAME,
                "repo": GITHUB_REPO,
            })
            worker_service = result["serviceCreate"]
            print(f"✅ تم إنشاء worker [{worker_service['id'][:8]}]")
        else:
            print("(dry-run) سيتم إنشاء worker")

    # ── 7. Get production environment ─────────────────────────────────────────
    separator("7. بيئة production")
    envs = gql(token, Q_ENVIRONMENTS, {"projectId": project_id})
    env_list = [e["node"] for e in envs["environments"]["edges"]]

    prod_env = None
    for env in env_list:
        print(f"  بيئة: {env['name']} [{env['id'][:8]}]")
        if env["name"].lower() == "production":
            prod_env = env

    if prod_env is None and env_list:
        prod_env = env_list[0]
        print(f"⚠️  لا توجد بيئة production، سنستخدم: {prod_env['name']}")
    elif prod_env:
        print(f"✅ بيئة production موجودة")

    # ── 8. Check Volume ────────────────────────────────────────────────────────
    separator("8. التحقق من الـ Volume")
    volumes = gql(token, Q_VOLUMES, {"projectId": project_id})
    vol_list = [e["node"] for e in volumes["volumes"]["edges"]]

    bot_data_vol = None
    for v in vol_list:
        instances = [i["node"] for i in v["volumeInstances"]["edges"]]
        mounts = [i["mountPath"] for i in instances]
        print(f"  Volume: {v['name']} [{v['id'][:8]}] | mount paths: {mounts}")
        if v["name"] == VOLUME_NAME or MOUNT_PATH in mounts:
            bot_data_vol = v

    if bot_data_vol:
        # تحقق أن الـ volume مربوط بـ worker في production
        instances = [i["node"] for i in bot_data_vol["volumeInstances"]["edges"]]
        worker_attached = any(
            i.get("serviceId") == (worker_service["id"] if worker_service else None)
            for i in instances
        )
        if worker_attached:
            print(f"✅ Volume '{VOLUME_NAME}' مربوط بـ worker على {MOUNT_PATH}")
        else:
            print(f"⚠️  Volume موجود لكن غير مربوط بـ worker")
            print(f"   → قم يدوياً بـ: Railway Dashboard → bugksa-bot → Volume → Attach to worker, path: {MOUNT_PATH}")
    else:
        print(f"⚠️  Volume '{VOLUME_NAME}' غير موجود")
        if not dry_run and worker_service and prod_env:
            print(f"   → إنشاء Volume وربطه بـ worker ...")
            gql(token, M_CREATE_VOLUME, {
                "projectId": project_id,
                "name": VOLUME_NAME,
                "mountPath": MOUNT_PATH,
                "serviceId": worker_service["id"],
                "environmentId": prod_env["id"],
            })
            print(f"✅ تم إنشاء Volume '{VOLUME_NAME}' على {MOUNT_PATH}")
        else:
            print(f"   (dry-run أو بيانات ناقصة)")

    # ── 9. Check env variables ─────────────────────────────────────────────────
    separator("9. التحقق من متغيرات البيئة")

    if worker_service and prod_env:
        try:
            vars_data = gql(token, Q_ENV_VARS, {
                "serviceId": worker_service["id"],
                "environmentId": prod_env["id"],
            })
            existing_vars = set(vars_data.get("variables", {}).keys())

            all_present = True
            for var in REQUIRED_VARS:
                if var in existing_vars:
                    print(f"  ✅ {var}")
                else:
                    print(f"  ❌ {var} — مفقود! أضفه يدوياً في Railway Dashboard")
                    all_present = False

            if all_present:
                print("\n✅ جميع المتغيرات موجودة")
            else:
                print("\n⚠️  بعض المتغيرات مفقودة — أضفها في:")
                print(f"   Railway Dashboard → bugksa-bot → worker → Variables")
        except Exception as e:
            print(f"⚠️  لم نتمكن من قراءة المتغيرات: {e}")
    else:
        print("⚠️  لا يمكن التحقق بدون worker أو بيئة production")

    # ── 10. Final Report ───────────────────────────────────────────────────────
    separator("التقرير النهائي")

    # إعادة جلب المشاريع بعد كل التعديلات
    final_projects = gql(token, Q_PROJECTS)["projects"]["edges"]
    print(f"عدد المشاريع المتبقية: {len(final_projects)}")
    for e in final_projects:
        p = e["node"]
        svcs = [s["node"]["name"] for s in p["services"]["edges"]]
        print(f"  • {p['name']} [{p['id'][:8]}] → خدمات: {svcs or ['(لا توجد)']}")

    print()
    print("═" * 60)
    print("  الحالة النهائية:")
    print(f"  مشروع   : {PROJECT_NAME}")
    print(f"  خدمة    : {SERVICE_NAME}")
    print(f"  Volume   : {VOLUME_NAME} → {MOUNT_PATH}")
    print(f"  GitHub   : {GITHUB_REPO}")
    print("═" * 60)
    print("\n✅ اكتمل التنظيف بنجاح" if not dry_run else "\n[DRY RUN] اكتمل الفحص")


if __name__ == "__main__":
    main()
