"""Record fixtures -- trimmed copies of real stored records, not invented ones.

The sibling OAI project lost a whole mapping to a fixture whose field names did
not exist in the real export; a fixture that is *better* than the data hides bugs
in the data. So these keep the real element names, the real values, and -- where
it matters most -- the real *shape*.

They live in a module rather than in `conftest.py` so both the fixture users and
the route tests can import the same strings.
"""

from __future__ import annotations

# sync_Object/Object-447469.xml, trimmed to the elements the mapping reads.
# Values are verbatim.
REAL_OBJECT = """\
<application><modules><module name="Object" totalSize="1000">
  <moduleItem hasAttachments="true" id="447469">
    <systemField dataType="Varchar" name="__orgUnit"><value>KKZusatzmagazin</value></systemField>
    <systemField dataType="Timestamp" name="__lastModified">
      <value>2026-08-24 07:11:33.54</value>
    </systemField>
    <dataField dataType="Clob" name="ObjTechnicalTermClb"><value>Zeichnung</value></dataField>
    <dataField dataType="Varchar" name="ObjObjectNumberTxt"><value>KdZ 5653</value></dataField>
    <dataField dataType="Varchar" name="ObjObjectNumberSortedTxt"><value>KdZ 005653</value></dataField>
    <vocabularyReference name="ObjNormalLocationVoc" id="30283">
      <vocabularyReferenceItem id="5023149">
        <formattedValue language="de">FT 5/10</formattedValue>
      </vocabularyReferenceItem>
    </vocabularyReference>
    <vocabularyReference name="ObjOrgGroupVoc" id="61643">
      <vocabularyReferenceItem id="1632860" name="KK-Fran/Span/Eng Zeichnung">
        <formattedValue language="de">KK-Fran/Span/Eng Zeichnung</formattedValue>
      </vocabularyReferenceItem>
    </vocabularyReference>
    <virtualField name="ObjObjectTitleVrt">
      <value>Sitzende junge Frau ("Briefleserin")</value>
    </virtualField>
    <virtualField name="ObjDateVrt"><value>um 1715/16</value></virtualField>
    <virtualField name="ObjMaterialTechniqueVrt">
      <value>Rötel, schwarze und weiße Kreide, auf graubraunem Naturpapier</value>
    </virtualField>
    <virtualField name="ObjPerAssociationMainParticipantNameVrt">
      <value>Watteau, Antoine Jean</value>
    </virtualField>
    <virtualField name="ObjPerAssociationMainParticipantVrt">
      <value>Jean Antoine Watteau (1684-1721)</value>
    </virtualField>
    <virtualField name="ObjPerAssociationSurnameVrt"><value/></virtualField>
    <virtualField name="ObjPerAssociationGenderVrt"><value/></virtualField>
    <moduleReference name="ObjPerAssociationRef" targetModule="Person" multiplicity="M:N" size="1">
      <moduleReferenceItem moduleItemId="218316" seqNo="0">
        <formattedValue language="de">Ausführung: Jean Antoine Watteau (1684-1721), Zeichner*in</formattedValue>
        <vocabularyReference name="RoleVoc" id="30423">
          <vocabularyReferenceItem id="4378474" name="Zeichner">
            <formattedValue language="de">Zeichner*in</formattedValue>
          </vocabularyReferenceItem>
        </vocabularyReference>
      </moduleReferenceItem>
    </moduleReference>
    <moduleReference name="ObjOwnerRef" targetModule="Address" multiplicity="M:N" size="1">
      <moduleReferenceItem moduleItemId="112264" seqNo="0">
        <formattedValue language="de">Kupferstichkabinett, Staatliche Museen zu Berlin</formattedValue>
      </moduleReferenceItem>
    </moduleReference>
    <moduleReference name="ObjMultimediaRef" targetModule="Multimedia" multiplicity="1:N" size="3">
      <moduleReferenceItem moduleItemId="636116" seqNo="0"/>
      <moduleReferenceItem moduleItemId="4014992" seqNo="1"/>
      <moduleReferenceItem moduleItemId="5179622" seqNo="2"/>
    </moduleReference>
  </moduleItem>
</module></modules></application>
"""

# A record with a real empty title, which the export does emit: 611 of 47,853.
UNTITLED_OBJECT = """\
<application><modules><module name="Object">
  <moduleItem id="999999">
    <virtualField name="ObjObjectTitleVrt"><value/></virtualField>
    <dataField name="ObjObjectNumberTxt"><value>KdZ 99999</value></dataField>
  </moduleItem>
</module></modules></application>
"""

# sync_Multimedia/Multimedia-268711.xml, trimmed. The important part is the
# SHAPE: its only cross-module link is nested at
#   moduleItem > composite[@name=MulReferencesCre] > compositeItem > moduleReference
# whereas Object keeps its references as direct children of moduleItem. Searching
# direct children therefore finds links for Objects and none for Assets -- and
# zero links is a valid answer, so it failed silently for 127,551 records.
REAL_MULTIMEDIA = """\
<application><modules><module name="Multimedia" totalSize="4232">
  <moduleItem hasAttachments="true" id="268711">
    <systemField dataType="Varchar" name="__orgUnit"><value>KKDeutscheDruckgraphik16Jh</value></systemField>
    <systemField dataType="Timestamp" name="__lastModified">
      <value>2025-05-14 08:48:31.599</value>
    </systemField>
    <dataField dataType="Varchar" name="MulOriginalFileTxt"><value>20001815-p.jpg</value></dataField>
    <dataField dataType="Varchar" name="MulSizeTxt"><value>1.99 MB</value></dataField>
    <vocabularyReference name="MulTypeVoc" id="1">
      <vocabularyReferenceItem id="2" name="image">
        <formattedValue language="de">image</formattedValue>
      </vocabularyReferenceItem>
    </vocabularyReference>
    <composite name="MulReferencesCre">
      <compositeItem>
        <moduleReference name="MulObjectRef" targetModule="Object" multiplicity="M:N" size="1">
          <moduleReferenceItem moduleItemId="449615" seqNo="0">
            <formattedValue language="de">KdZ 5653</formattedValue>
            <dataField dataType="Long" name="SortLnu"><value>5</value></dataField>
            <vocabularyReference name="RoleVoc" id="3">
              <vocabularyReferenceItem id="4" name="Fotograf">
                <formattedValue language="de">Fotograf*in</formattedValue>
              </vocabularyReferenceItem>
            </vocabularyReference>
          </moduleReferenceItem>
        </moduleReference>
      </compositeItem>
    </composite>
  </moduleItem>
</module></modules></application>
"""
