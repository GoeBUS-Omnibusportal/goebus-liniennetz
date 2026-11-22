-- GöBUS Fassung 61: Bus-Profil für den eigenen Routenplaner (OSRM) im GitHub-Lauf.
-- Der Workflow fügt diesen Teil vor dem abschließenden „return {“ in eine Kopie von car.lua ein (bus.lua).
-- Unterschiede zum Auto-Profil:
--   * Zugangsregeln zuerst für Busse: bus, psv (öffentlicher Verkehr), dann motor_vehicle, vehicle, access –
--     Busschleusen und Busspuren („motor_vehicle=no + bus=yes“) sind befahrbar, „bus=no“ ist gesperrt.
--   * „psv“, „bus“ und „minibus“ stehen nicht mehr auf der Sperrliste (die übrigen Sperren von car.lua bleiben).
--   * Fassung 63: Höhenbegrenzung unter 3,2 m ist gesperrt; Gewichtsbegrenzung unter 18 t und Höhe 3,2–3,6 m werden nur
--     gemieden (vierfache Bewertung), außer sie sind für Busse freigegeben. Bis Fassung 62 waren sie gesperrt – Ortsdurchfahrten
--     wie Ertinghausen (Linie 222) waren dann nur über weite Umwege erreichbar, die Skizze wurde verworfen und blieb gerade:
--     „Linienverkehr frei“ steht an vielen Schildern, in OpenStreetMap aber selten.
local goebus_setup, goebus_process_way = setup, process_way

function setup()
  local p = goebus_setup()
  p.access_tags_hierarchy = Sequence { 'bus', 'psv', 'motor_vehicle', 'vehicle', 'access' }
  -- car.lua sperrt „psv“, „bus“, „minibus“ (geprüft an car.lua von Project-OSRM, Stand 22.11.2026) – für Busse freigeben
  p.access_tag_blacklist['psv'] = nil
  p.access_tag_blacklist['bus'] = nil
  p.access_tag_blacklist['minibus'] = nil
  return p
end

local function goebus_zahl(v)
  if v == nil then return nil end
  return tonumber(string.match(tostring(v), '^%s*([%d%.]+)'))
end

function process_way(profile, way, result, relations)
  goebus_process_way(profile, way, result, relations)
  local bus = way:get_value_by_key('bus') or way:get_value_by_key('psv')
  if bus == 'yes' or bus == 'designated' then return end
  local w, h = goebus_zahl(way:get_value_by_key('maxweight')), goebus_zahl(way:get_value_by_key('maxheight'))
  if h and h < 3.2 then
    result.forward_mode = mode.inaccessible
    result.backward_mode = mode.inaccessible
  elseif (w and w < 18) or (h and h < 3.6) then
    if result.forward_rate and result.forward_rate > 0 then result.forward_rate = result.forward_rate / 4 end
    if result.backward_rate and result.backward_rate > 0 then result.backward_rate = result.backward_rate / 4 end
  end
end
