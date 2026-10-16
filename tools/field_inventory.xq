(: Field inventory for the MuseumPlus RIA store — one pass, with per-RECORD coverage.
 :
 : The version this replaces counted each name by re-scanning the collection per name,
 : which is O(records x names) and does not finish on 182k records. This groups in a
 : single pass instead.
 :
 : `n`       = occurrences of the name (a repeatable field counts once per repetition)
 : `records` = how many moduleItems actually carry it -- this is the number that decides
 :             whether a field is worth putting on a record page or a facet, because a
 :             field on 3% of records is a mostly-empty display line.
 :
 : Run:  cd ~/basex/basex && ./bin/basex ~/py/myfind/tools/field_inventory.xq > inventory.xml
 :)
let $dbs := map {
  'Object': 'sync_Object',
  'Person': 'sync_Person',
  'Multimedia': 'sync_Multimedia'
}
let $kinds := ('dataField', 'virtualField', 'systemField', 'vocabularyReference',
               'moduleReference', 'composite', 'repeatableGroup')
return element inventory {
  for $module in map:keys($dbs)
  let $items := collection($dbs($module))//moduleItem
  return element module {
    attribute name { $module },
    attribute items { count($items) },
    for $n in $items//*[local-name() = $kinds]
    let $kind := local-name($n)
    let $nm := string($n/@name)
    where $nm ne ''
    group by $kind, $nm
    order by $kind, $nm
    return element f {
      attribute kind { $kind },
      attribute name { $nm },
      attribute n { count($n) },
      attribute records { count(distinct-values($n/ancestor::moduleItem[1]/@id)) }
    }
  }
}
