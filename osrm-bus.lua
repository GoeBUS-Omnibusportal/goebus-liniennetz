-- GöBUS Fassung 61: Bus-Profil für den eigenen Routenplaner (OSRM) im GitHub-Lauf.
-- Der Workflow fügt diesen Teil vor dem abschließenden „return {“ in eine Kopie von car.lua ein (bus.lua).
-- Unterschiede zum Auto-Profil:
--   * Zugangsregeln zuerst für Busse: bus, psv (öffentlicher Verkehr), dann motor_vehicle, vehicle, access –
--     Busschleusen und Busspuren („motor_vehicle=no + bus=yes“) sind befahrbar, „bus=no“ ist gesperrt.
--   * „psv“, „bus“ und „minibus“ stehen nicht mehr auf der Sperrliste (die übrigen Sperren von car.lua bleiben).
--   * Straßen mit Gewichts- unter 18 t oder Höhenbegrenzung unter 3,6 m sind gesperrt, außer sie sind für Busse freigegeben.
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
  if (w and w < 18) or (h and h < 3.6) then
    result.forward_mode = mode.inaccessible
    result.backward_mode = mode.inaccessible
  end
end
