const hosts = process.env.MONGO_HOSTS.split(",");
const deadline = Date.now() + 240000;

for (const host of hosts) {
  while (true) {
    try {
      const node = new Mongo(`mongodb://${host}/?directConnection=true&serverSelectionTimeoutMS=2000`);
      const result = node.getDB("admin").runCommand({ ping: 1 });
      if (result.ok !== 1) throw new Error("MongoDB ping failed");
      break;
    } catch (error) {
      if (Date.now() >= deadline) throw error;
      print(`Waiting for ${host}`);
      sleep(2000);
    }
  }
}

const admin = new Mongo(
  `mongodb://${hosts[0]}/?directConnection=true&serverSelectionTimeoutMS=2000`
).getDB("admin");
try {
  admin.runCommand({ replSetGetStatus: 1 });
} catch (error) {
  if (error.code !== 94) throw error;
  const result = admin.runCommand({
    replSetInitiate: {
      _id: "rs0",
      members: hosts.map((host, index) => ({
        _id: index,
        host,
        priority: index === 0 ? 2 : 1,
      })),
    },
  });
  if (result.ok !== 1) throw new Error(`Cannot initialize replica set: ${result.code}`);
}

const existing = admin.runCommand({ replSetGetConfig: 1 });
if (existing.ok !== 1 || existing.config._id !== "rs0" ||
    JSON.stringify(existing.config.members.map((member) => member.host).sort()) !==
    JSON.stringify([...hosts].sort())) {
  throw new Error("Replica-set membership does not match the configured hosts");
}

let ready = false;
while (Date.now() < deadline) {
  const current = admin.runCommand({ replSetGetStatus: 1 });
  if (current.ok === 1 &&
      current.members.filter((member) => member.health === 1 && member.state === 1).length === 1 &&
      current.members.filter((member) => member.health === 1 && member.state === 2).length === 2) {
    ready = true;
    break;
  }
  sleep(2000);
}
if (!ready) throw new Error("Replica set did not become ready");
print("Replica set has a primary and two secondaries");
