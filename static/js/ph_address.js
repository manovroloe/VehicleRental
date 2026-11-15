
const PSGC_BASE = "https://psgc.cloud/api/v2";

const fallbackAddress = {
  regions: [
    {code: "0700000000", name: "Region VII (Central Visayas)"}
  ],
  provinces: {
    "0700000000": [{code: "0722000000", name: "Cebu"}]
  },
  cities: {
    "0722000000": [
      {code: "0722170000", name: "Cebu City"},
      {code: "0722260000", name: "Lapu-Lapu City"},
      {code: "0722300000", name: "Mandaue City"},
      {code: "0722110000", name: "Danao City"},
      {code: "0722460000", name: "Talisay City"}
    ]
  },
  barangays: {
    "0722170000": ["Apas","Banilad","Basak San Nicolas","Guadalupe","Lahug","Mabolo","Talamban"],
    "0722260000": ["Basak","Mactan","Maribago","Pajo","Pusok"],
    "0722300000": ["Bakilid","Banilad","Cabancalan","Centro","Tipolo"],
    "0722110000": ["Poblacion","Guinsay","Sabang"],
    "0722460000": ["Bulacao","Lagtang","Lawaan I","Poblacion"]
  }
};

async function getJSON(url) {
  const response = await fetch(url, {headers: {"Accept": "application/json"}});
  if (!response.ok) throw new Error("Address service unavailable");
  return await response.json();
}

function setOptions(select, items, placeholder) {
  select.innerHTML = `<option value="">${placeholder}</option>`;
  items.forEach(item => {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.name;
    option.dataset.code = item.code || "";
    select.appendChild(option);
  });
}

function selectByName(select, name) {
  if (!name) return false;
  const option = [...select.options].find(o => o.value === name);
  if (option) {
    select.value = name;
    return true;
  }
  return false;
}

async function loadRegions(group) {
  const region = group.querySelector(".address-region");
  let items;
  try {
    items = await getJSON(`${PSGC_BASE}/regions`);
  } catch (e) {
    items = fallbackAddress.regions;
  }
  setOptions(region, items, "Select region");
}

async function loadProvinces(group) {
  const region = group.querySelector(".address-region");
  const province = group.querySelector(".address-province");
  const city = group.querySelector(".address-city");
  const barangay = group.querySelector(".address-barangay");

  setOptions(city, [], "Select province first");
  setOptions(barangay, [], "Select city first");

  const selected = region.selectedOptions[0];
  const code = selected?.dataset.code;
  if (!code) return setOptions(province, [], "Select region first");

  let items = [];
  try {
    items = await getJSON(`${PSGC_BASE}/regions/${encodeURIComponent(code)}/provinces`);
  } catch (e) {
    items = fallbackAddress.provinces[code] || [];
  }

  if (!items.length) {
    // Regions such as NCR do not have a province layer.
    items = [{code: "", name: selected.value, regionCode: code}];
  }

  setOptions(province, items, "Select province");
  if (!items[0].code) {
    province.options[1].dataset.regionCode = code;
  }
}

async function loadCities(group) {
  const region = group.querySelector(".address-region");
  const province = group.querySelector(".address-province");
  const city = group.querySelector(".address-city");
  const barangay = group.querySelector(".address-barangay");

  setOptions(barangay, [], "Select city first");

  const provinceOption = province.selectedOptions[0];
  const provinceCode = provinceOption?.dataset.code || "";
  const regionCode = region.selectedOptions[0]?.dataset.code || "";

  if (!province.value) return setOptions(city, [], "Select province first");

  let items = [];
  try {
    const url = provinceCode
      ? `${PSGC_BASE}/provinces/${encodeURIComponent(provinceCode)}/cities-municipalities`
      : `${PSGC_BASE}/regions/${encodeURIComponent(regionCode)}/cities-municipalities`;
    items = await getJSON(url);
  } catch (e) {
    items = fallbackAddress.cities[provinceCode] || [];
  }

  setOptions(city, items, "Select city / municipality");
}

async function loadBarangays(group) {
  const city = group.querySelector(".address-city");
  const barangay = group.querySelector(".address-barangay");
  const cityCode = city.selectedOptions[0]?.dataset.code;

  if (!cityCode) return setOptions(barangay, [], "Select city first");

  let items = [];
  try {
    items = await getJSON(`${PSGC_BASE}/cities-municipalities/${encodeURIComponent(cityCode)}/barangays`);
  } catch (e) {
    items = (fallbackAddress.barangays[cityCode] || []).map((name, index) => ({code: String(index), name}));
  }

  setOptions(barangay, items, "Select barangay");
}

async function restoreGroup(group) {
  const saved = {
    region: group.dataset.currentRegion || "",
    province: group.dataset.currentProvince || "",
    city: group.dataset.currentCity || "",
    barangay: group.dataset.currentBarangay || ""
  };

  await loadRegions(group);
  if (selectByName(group.querySelector(".address-region"), saved.region)) {
    await loadProvinces(group);
    if (selectByName(group.querySelector(".address-province"), saved.province)) {
      await loadCities(group);
      if (selectByName(group.querySelector(".address-city"), saved.city)) {
        await loadBarangays(group);
        selectByName(group.querySelector(".address-barangay"), saved.barangay);
      }
    }
  }
}

async function copyPermanentToStaying() {
  const permanent = document.querySelector('[data-prefix="permanent"]');
  const staying = document.querySelector('[data-prefix="staying"]');
  if (!permanent || !staying) return;

  const regionName = permanent.querySelector(".address-region").value;
  const provinceName = permanent.querySelector(".address-province").value;
  const cityName = permanent.querySelector(".address-city").value;
  const barangayName = permanent.querySelector(".address-barangay").value;

  await loadRegions(staying);
  selectByName(staying.querySelector(".address-region"), regionName);
  await loadProvinces(staying);
  selectByName(staying.querySelector(".address-province"), provinceName);
  await loadCities(staying);
  selectByName(staying.querySelector(".address-city"), cityName);
  await loadBarangays(staying);
  selectByName(staying.querySelector(".address-barangay"), barangayName);

  const permanentStreet = document.querySelector('[name="permanent_street"]');
  const stayingStreet = document.querySelector('[name="staying_street"]');
  if (permanentStreet && stayingStreet) stayingStreet.value = permanentStreet.value;
}

document.addEventListener("DOMContentLoaded", async () => {
  const groups = [...document.querySelectorAll(".address-group")];

  for (const group of groups) {
    const region = group.querySelector(".address-region");
    const province = group.querySelector(".address-province");
    const city = group.querySelector(".address-city");

    region.addEventListener("change", () => loadProvinces(group));
    province.addEventListener("change", () => loadCities(group));
    city.addEventListener("change", () => loadBarangays(group));

    await restoreGroup(group);
  }

  document.getElementById("copyPermanentAddress")?.addEventListener("click", copyPermanentToStaying);
});
