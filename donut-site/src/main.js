import * as THREE from 'three';

const canvas = document.getElementById('scene');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a0f);
scene.fog = new THREE.Fog(0x0a0a0f, 8, 16);

const camera = new THREE.PerspectiveCamera(
  45,
  window.innerWidth / window.innerHeight,
  0.1,
  100
);
camera.position.set(0, 0, 6);

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.shadowMap.enabled = true;

// Lights
scene.add(new THREE.AmbientLight(0xffffff, 0.35));

const keyLight = new THREE.PointLight(0xffe9c7, 2.2, 20);
keyLight.position.set(4, 5, 5);
keyLight.castShadow = true;
scene.add(keyLight);

const rimLight = new THREE.PointLight(0xff6fae, 1.4, 20);
rimLight.position.set(-5, -3, -4);
scene.add(rimLight);

const fillLight = new THREE.DirectionalLight(0xffffff, 0.4);
fillLight.position.set(-3, 4, 2);
scene.add(fillLight);

// The donut group — everything spins together
const donut = new THREE.Group();
scene.add(donut);

// Dough (torus base)
const doughGeometry = new THREE.TorusGeometry(1.4, 0.65, 64, 128);
const doughMaterial = new THREE.MeshStandardMaterial({
  color: 0xc98a4b,
  roughness: 0.75,
  metalness: 0.05,
});
const dough = new THREE.Mesh(doughGeometry, doughMaterial);
dough.castShadow = true;
dough.receiveShadow = true;
donut.add(dough);

// Icing (slightly smaller torus, offset so it sits on "top" of the dough)
const icingGeometry = new THREE.TorusGeometry(1.4, 0.72, 64, 128, Math.PI * 2);
const icingMaterial = new THREE.MeshStandardMaterial({
  color: 0xff6fae,
  roughness: 0.35,
  metalness: 0.1,
});
const icing = new THREE.Mesh(icingGeometry, icingMaterial);
icing.scale.set(1, 1, 0.55);
icing.position.z = 0.28;
icing.castShadow = true;
icing.receiveShadow = true;
donut.add(icing);

// Sprinkles — small capsules scattered around the icing torus
const sprinkleColors = [0xffffff, 0x5de1ff, 0xffe45e, 0x7cff6b, 0xffffff, 0xff4d4d];
const sprinkleGeometry = new THREE.CapsuleGeometry(0.035, 0.16, 2, 6);
const sprinkleCount = 140;

for (let i = 0; i < sprinkleCount; i++) {
  const material = new THREE.MeshStandardMaterial({
    color: sprinkleColors[i % sprinkleColors.length],
    roughness: 0.4,
  });
  const sprinkle = new THREE.Mesh(sprinkleGeometry, material);

  // Random position on the torus surface (icing side)
  const theta = Math.random() * Math.PI * 2; // around the big ring
  const phi = Math.random() * Math.PI * 2; // around the tube
  const R = 1.4;
  const r = 0.5 + Math.random() * 0.35;

  const x = (R + r * Math.cos(phi)) * Math.cos(theta);
  const y = (R + r * Math.cos(phi)) * Math.sin(theta);
  const z = r * Math.sin(phi) * 0.55 + 0.28;

  sprinkle.position.set(x, y, z);
  sprinkle.lookAt(x * 1.5, y * 1.5, z * 1.5);
  sprinkle.rotation.z += Math.random() * Math.PI * 2;
  sprinkle.castShadow = true;

  donut.add(sprinkle);
}

// Resize handling
function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}
window.addEventListener('resize', onResize);

// Animation loop — tumble on all three axes so it rotates through every angle
const clock = new THREE.Clock();

function animate() {
  const t = clock.getElapsedTime();

  donut.rotation.x = t * 0.6;
  donut.rotation.y = t * 0.9;
  donut.rotation.z = t * 0.35;

  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}

animate();
